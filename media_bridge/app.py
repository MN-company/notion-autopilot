from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from pathlib import Path

import httpx
import fitz  # PyMuPDF
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

app = FastAPI(title="Notion Autopilot Media Bridge", version="1.0.0")


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


NOTION_TOKEN = _env("NOTION_TOKEN", required=False)
NOTION_VERSION = _env("NOTION_VERSION", "2022-06-28") or "2022-06-28"
ALLOWED_DOWNLOAD_HOSTS = {
    h.strip().lower()
    for h in (_env("ALLOWED_DOWNLOAD_HOSTS", "files.openai.com,files.oaiusercontent.com,files.openaiusercontent.com") or "")
    .split(",")
    if h.strip()
}
ALLOWED_DOWNLOAD_HOST_SUFFIXES = {
    s.strip().lower().lstrip(".")
    for s in (_env("ALLOWED_DOWNLOAD_HOST_SUFFIXES", "oaiusercontent.com,openaiusercontent.com") or "").split(",")
    if s.strip()
}
MAX_DOWNLOAD_BYTES = int(_env("MAX_DOWNLOAD_BYTES", str(30 * 1024 * 1024)) or str(30 * 1024 * 1024))
MAX_SLIDE_PAGES = int(_env("MAX_SLIDE_PAGES", "80") or "80")
SLIDE_RENDER_DPI = int(_env("SLIDE_RENDER_DPI", "150") or "150")
PDF_EXTRACT_MODE = (_env("PDF_EXTRACT_MODE", "none") or "none").strip().lower()
NOTION_OAUTH_AUTHORIZE_URL = _env("NOTION_OAUTH_AUTHORIZE_URL", "https://api.notion.com/v1/oauth/authorize")
NOTION_OAUTH_TOKEN_URL = _env("NOTION_OAUTH_TOKEN_URL", "https://api.notion.com/v1/oauth/token")
NOTION_OAUTH_CLIENT_ID = _env("NOTION_OAUTH_CLIENT_ID", required=False)
NOTION_OAUTH_CLIENT_SECRET = _env("NOTION_OAUTH_CLIENT_SECRET", required=False)
MAX_INLINE_FILE_BYTES = int(_env("MAX_INLINE_FILE_BYTES", str(8 * 1024 * 1024)) or str(8 * 1024 * 1024))


def _validate_download_link(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="download_link must use https.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="download_link is missing a hostname.")
    host_ok = host in ALLOWED_DOWNLOAD_HOSTS or any(
        host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_DOWNLOAD_HOST_SUFFIXES
    )
    if not host_ok:
        raise HTTPException(
            status_code=400,
            detail=(
                f"download_link host '{host}' is not allowed. "
                "Set ALLOWED_DOWNLOAD_HOSTS or ALLOWED_DOWNLOAD_HOST_SUFFIXES to include it."
            ),
        )


async def _download_to_tempfile(url: str) -> tuple[str, int]:
    _validate_download_link(url)
    async with httpx.AsyncClient(timeout=45) as client:
        async with client.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False) as f:
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large for media bridge (max {MAX_DOWNLOAD_BYTES} bytes).",
                        )
                    f.write(chunk)
                return f.name, size


def _cleanup_temp_artifacts(file_paths: list[str], dir_paths: list[str]) -> None:
    for p in set(file_paths):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    for d in set(dir_paths):
        shutil.rmtree(d, ignore_errors=True)


def _is_pdf(name: str, mime_type: str) -> bool:
    return mime_type == "application/pdf" or name.lower().endswith(".pdf")


def _is_presentation(name: str, mime_type: str) -> bool:
    lower = name.lower()
    if lower.endswith(".pptx") or lower.endswith(".ppt"):
        return True
    return mime_type in {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    }


def _render_pdf_to_pngs(pdf_path: str, source_name: str) -> list[dict[str, str]]:
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise HTTPException(status_code=400, detail="The provided PDF has no pages.")
        if doc.page_count > MAX_SLIDE_PAGES:
            raise HTTPException(
                status_code=413,
                detail=f"PDF has {doc.page_count} pages, limit is {MAX_SLIDE_PAGES}.",
            )
        zoom = SLIDE_RENDER_DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        base = Path(source_name).stem or "slides"
        images: list[dict[str, str]] = []
        for idx in range(doc.page_count):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as out:
                out_path = out.name
            pix.save(out_path)
            images.append(
                {
                    "path": out_path,
                    "name": f"{base}_slide_{idx + 1:03d}.png",
                    "mime_type": "image/png",
                }
            )
        return images
    finally:
        doc.close()


def _convert_presentation_to_pdf(input_path: str) -> tuple[str, str]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise HTTPException(
            status_code=422,
            detail="PPT/PPTX conversion requires LibreOffice (`soffice`) on the bridge runtime. "
            "Upload a PDF deck or install LibreOffice in the container.",
        )
    out_dir = tempfile.mkdtemp(prefix="pptx_to_pdf_")
    try:
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, input_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        raise HTTPException(
            status_code=500,
            detail=f"PPT/PPTX conversion failed. stdout={stdout[:500]} stderr={stderr[:500]}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="PPT/PPTX conversion timed out.") from exc

    candidates = sorted(Path(out_dir).glob("*.pdf"))
    if not candidates:
        # LibreOffice sometimes emits success text without output when input is malformed.
        output = (result.stdout or "").strip()
        raise HTTPException(status_code=500, detail=f"PPT/PPTX conversion produced no PDF. {output[:500]}")
    return str(candidates[0]), out_dir


def _expand_downloaded_file(
    file_path: str, name: str, mime_type: str
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    cleanup_files = [file_path]
    cleanup_dirs: list[str] = []

    if _is_pdf(name, mime_type):
        if PDF_EXTRACT_MODE == "page":
            images = _render_pdf_to_pngs(file_path, name)
            cleanup_files.extend([img["path"] for img in images])
            return images, cleanup_files, cleanup_dirs
        # Default behavior is no extraction for PDFs.
        return [{"path": file_path, "name": name, "mime_type": mime_type}], cleanup_files, cleanup_dirs

    if _is_presentation(name, mime_type):
        if PDF_EXTRACT_MODE == "page":
            pdf_path, tmp_dir = _convert_presentation_to_pdf(file_path)
            cleanup_dirs.append(tmp_dir)
            images = _render_pdf_to_pngs(pdf_path, name)
            cleanup_files.extend([img["path"] for img in images])
            return images, cleanup_files, cleanup_dirs
        return [{"path": file_path, "name": name, "mime_type": mime_type}], cleanup_files, cleanup_dirs

    return [{"path": file_path, "name": name, "mime_type": mime_type}], cleanup_files, cleanup_dirs


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def _notion_token_from_request(authorization: str | None) -> str:
    # Prefer server-configured integration token for stable single-workspace deployments.
    # This avoids accidentally forwarding unrelated bearer tokens (for example Google OAuth tokens).
    if NOTION_TOKEN:
        return NOTION_TOKEN
    # Fallback to per-request bearer token when NOTION_TOKEN is not configured.
    token = _extract_bearer(authorization)
    if token:
        return token
    raise HTTPException(
        status_code=401,
        detail="Missing Notion token. Provide Authorization: Bearer <notion_token> or configure NOTION_TOKEN on the service.",
    )


def _openai_files_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    refs = body.get("openaiFileIdRefs") or []
    if not isinstance(refs, list):
        raise HTTPException(status_code=400, detail="openaiFileIdRefs must be an array.")
    if len(refs) == 0:
        raise HTTPException(
            status_code=400,
            detail="No files received. Attach at least one file in chat and call the action with openaiFileIdRefs.",
        )
    # At runtime, OpenAI populates this with objects (name, id, mime_type, download_link).
    # In the schema it's typed as strings, so accept both.
    normalized: list[dict[str, Any]] = []
    for item in refs:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            raise HTTPException(status_code=400, detail="openaiFileIdRefs must contain objects at runtime.")
    return normalized


def _inline_files_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    files = body.get("files") or []
    if not isinstance(files, list):
        raise HTTPException(status_code=400, detail="files must be an array.")
    if len(files) == 0:
        raise HTTPException(status_code=400, detail="No inline files provided.")

    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"files[{idx}] must be an object.")
        name = item.get("name") or f"inline_{idx + 1}"
        mime_type = item.get("mime_type") or "application/octet-stream"
        data_base64 = item.get("data_base64")
        if not isinstance(data_base64, str) or not data_base64.strip():
            raise HTTPException(status_code=400, detail=f"files[{idx}].data_base64 is required.")
        normalized.append({"name": name, "mime_type": mime_type, "data_base64": data_base64.strip()})
    return normalized


def _decode_inline_file_to_tempfile(data_base64: str) -> tuple[str, int]:
    try:
        raw = base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 payload.") from exc
    size = len(raw)
    if size == 0:
        raise HTTPException(status_code=400, detail="Inline file is empty.")
    if size > MAX_INLINE_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Inline file too large ({size} bytes). Max is {MAX_INLINE_FILE_BYTES} bytes.",
        )
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(raw)
        return f.name, size


async def _upload_file_to_notion(
    client: httpx.AsyncClient,
    notion_token: str,
    file_path: str,
    name: str,
    mime_type: str,
) -> str:
    create_resp = await client.post(
        "https://api.notion.com/v1/file_uploads",
        headers={
            "Authorization": f"Bearer {notion_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json={"mode": "single_part", "filename": name, "content_type": mime_type},
    )
    if create_resp.status_code >= 400:
        raise HTTPException(status_code=create_resp.status_code, detail=create_resp.text)
    upload_obj = create_resp.json()
    upload_id = upload_obj.get("id")
    if not upload_id:
        raise HTTPException(status_code=502, detail="Notion did not return a file upload id.")

    with open(file_path, "rb") as fp:
        send_resp = await client.post(
            f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
            headers={
                "Authorization": f"Bearer {notion_token}",
                "Notion-Version": NOTION_VERSION,
            },
            files={"file": (name, fp, mime_type)},
        )
    if send_resp.status_code >= 400:
        raise HTTPException(status_code=send_resp.status_code, detail=send_resp.text)
    return upload_id


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/oauth/notion/authorize")
async def oauth_notion_authorize(request: Request) -> RedirectResponse:
    params = dict(request.query_params)
    params.setdefault("owner", "user")
    params.setdefault("response_type", "code")
    if NOTION_OAUTH_CLIENT_ID:
        params["client_id"] = NOTION_OAUTH_CLIENT_ID
    redirect_to = f"{NOTION_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
    return RedirectResponse(url=redirect_to, status_code=302)


@app.post("/oauth/notion/token")
async def oauth_notion_token(request: Request) -> Response:
    body = await request.body()
    content_type = (request.headers.get("content-type") or "application/json").split(";", 1)[0].strip().lower()

    # The GPT OAuth exchange often sends x-www-form-urlencoded; Notion expects JSON.
    if content_type == "application/x-www-form-urlencoded":
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        payload = {k: v[-1] for k, v in parsed.items()}
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"

    forward_headers = {
        "Content-Type": content_type,
        "Notion-Version": NOTION_VERSION,
    }

    if NOTION_OAUTH_CLIENT_ID and NOTION_OAUTH_CLIENT_SECRET:
        basic = base64.b64encode(f"{NOTION_OAUTH_CLIENT_ID}:{NOTION_OAUTH_CLIENT_SECRET}".encode("utf-8")).decode(
            "utf-8"
        )
        forward_headers["Authorization"] = f"Basic {basic}"
    else:
        auth_header = request.headers.get("authorization")
        if auth_header:
            forward_headers["Authorization"] = auth_header

    async with httpx.AsyncClient(timeout=45) as client:
        token_resp = await client.post(
            NOTION_OAUTH_TOKEN_URL,
            headers=forward_headers,
            content=body,
        )

    return Response(
        content=token_resp.content,
        status_code=token_resp.status_code,
        media_type=token_resp.headers.get("content-type", "application/json"),
    )


@app.post("/v1/notion/file_uploads")
async def notion_file_uploads(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Upload files to Notion via the Direct Upload lifecycle:
    1) POST /v1/file_uploads (create)
    2) POST /v1/file_uploads/{id}/send (multipart/form-data, file=@...)
    Returns file_upload IDs for attachment via Notion block/page APIs.
    """
    notion_token = _notion_token_from_request(authorization)
    files = _openai_files_from_body(body)

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=45) as client:
        for f in files:
            name = f.get("name") or "upload"
            mime_type = f.get("mime_type") or "application/octet-stream"
            download_link = f.get("download_link")
            if not download_link:
                raise HTTPException(status_code=400, detail="Missing download_link in openaiFileIdRefs item.")

            tmp_path, _size = await _download_to_tempfile(download_link)
            expanded_files, cleanup_files, cleanup_dirs = _expand_downloaded_file(tmp_path, name, mime_type)
            try:
                for item in expanded_files:
                    upload_name = item["name"]
                    upload_mime = item["mime_type"]
                    upload_path = item["path"]
                    upload_id = await _upload_file_to_notion(client, notion_token, upload_path, upload_name, upload_mime)

                    results.append(
                        {
                            "source_name": name,
                            "name": upload_name,
                            "mime_type": upload_mime,
                            "file_upload_id": upload_id,
                        }
                    )
            finally:
                _cleanup_temp_artifacts(cleanup_files, cleanup_dirs)

    return {"uploads": results}


@app.post("/v1/notion/file_uploads_from_data")
async def notion_file_uploads_from_data(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Upload base64-encoded file bytes to Notion.
    Intended for files created inside sandbox/code tools when `openaiFileIdRefs` are unavailable.
    """
    notion_token = _notion_token_from_request(authorization)
    files = _inline_files_from_body(body)

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=45) as client:
        for f in files:
            name = f["name"]
            mime_type = f["mime_type"]
            tmp_path, _size = _decode_inline_file_to_tempfile(f["data_base64"])
            cleanup_files = [tmp_path]
            cleanup_dirs: list[str] = []
            try:
                # Reuse PDF/presentation extraction path only when inline payload is a deck.
                expanded_files, extra_files, extra_dirs = _expand_downloaded_file(tmp_path, name, mime_type)
                cleanup_files.extend([p for p in extra_files if p != tmp_path])
                cleanup_dirs.extend(extra_dirs)
                for item in expanded_files:
                    upload_name = item["name"]
                    upload_mime = item["mime_type"]
                    upload_path = item["path"]
                    upload_id = await _upload_file_to_notion(client, notion_token, upload_path, upload_name, upload_mime)
                    results.append(
                        {
                            "source_name": name,
                            "name": upload_name,
                            "mime_type": upload_mime,
                            "file_upload_id": upload_id,
                        }
                    )
            finally:
                _cleanup_temp_artifacts(cleanup_files, cleanup_dirs)

    return {"uploads": results}


@app.post("/v1/drive/upload_public")
async def drive_upload_public(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Upload files to Google Drive (drive.file) and make them public.
    Requires: Authorization: Bearer <google_access_token>
    """
    if not _extract_bearer(authorization):
        raise HTTPException(status_code=401, detail="Missing Google OAuth access token (Authorization: Bearer ...).")
    access_token = _extract_bearer(authorization) or ""
    files = _openai_files_from_body(body)
    folder_name = body.get("folder_name") or "Notion Autopilot Media"

    async with httpx.AsyncClient(timeout=45) as client:
        headers = {"Authorization": f"Bearer {access_token}"}

        # Find folder
        folder_name_escaped = folder_name.replace("'", "\\'")
        q = (
            "mimeType='application/vnd.google-apps.folder' "
            f"and name='{folder_name_escaped}' and trashed=false"
        )
        list_resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": q, "fields": "files(id,name,modifiedTime)", "pageSize": 10},
        )
        if list_resp.status_code >= 400:
            raise HTTPException(status_code=list_resp.status_code, detail=list_resp.text)
        folder_files = (list_resp.json() or {}).get("files", [])
        folder_id = folder_files[0]["id"] if folder_files else None

        if not folder_id:
            create_folder = await client.post(
                "https://www.googleapis.com/drive/v3/files",
                headers={**headers, "Content-Type": "application/json"},
                json={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
                params={"fields": "id"},
            )
            if create_folder.status_code >= 400:
                raise HTTPException(status_code=create_folder.status_code, detail=create_folder.text)
            folder_id = (create_folder.json() or {}).get("id")

        results: list[dict[str, Any]] = []
        for f in files:
            name = f.get("name") or "upload"
            mime_type = f.get("mime_type") or "application/octet-stream"
            download_link = f.get("download_link")
            if not download_link:
                raise HTTPException(status_code=400, detail="Missing download_link in openaiFileIdRefs item.")

            tmp_path, _size = await _download_to_tempfile(download_link)
            expanded_files, cleanup_files, cleanup_dirs = _expand_downloaded_file(tmp_path, name, mime_type)
            try:
                for item in expanded_files:
                    upload_name = item["name"]
                    upload_mime = item["mime_type"]
                    upload_path = item["path"]
                    with open(upload_path, "rb") as fp:
                        file_bytes = fp.read()

                    boundary = "notion-autopilot-boundary"
                    metadata = {"name": upload_name, "parents": [folder_id]}
                    body_bytes = (
                        f"--{boundary}\r\n"
                        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                        f"{json.dumps(metadata)}\r\n"
                        f"--{boundary}\r\n"
                        f"Content-Type: {upload_mime}\r\n\r\n"
                    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

                    upload_resp = await client.post(
                        "https://www.googleapis.com/upload/drive/v3/files",
                        headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"},
                        params={"uploadType": "multipart", "fields": "id,webViewLink,webContentLink"},
                        content=body_bytes,
                    )
                    if upload_resp.status_code >= 400:
                        raise HTTPException(status_code=upload_resp.status_code, detail=upload_resp.text)
                    uploaded = upload_resp.json() or {}
                    file_id = uploaded.get("id")
                    if not file_id:
                        raise HTTPException(status_code=502, detail="Drive did not return a file id.")

                    perm_resp = await client.post(
                        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"type": "anyone", "role": "reader"},
                    )
                    if perm_resp.status_code >= 400:
                        raise HTTPException(status_code=perm_resp.status_code, detail=perm_resp.text)

                    meta_resp = await client.get(
                        f"https://www.googleapis.com/drive/v3/files/{file_id}",
                        headers=headers,
                        params={"fields": "id,name,webContentLink,webViewLink"},
                    )
                    if meta_resp.status_code >= 400:
                        raise HTTPException(status_code=meta_resp.status_code, detail=meta_resp.text)
                    meta = meta_resp.json() or {}
                    web_content = meta.get("webContentLink")
                    public_url = web_content or f"https://drive.google.com/uc?export=download&id={file_id}"

                    results.append(
                        {
                            "source_name": name,
                            "name": upload_name,
                            "mime_type": upload_mime,
                            "file_id": file_id,
                            "public_url": public_url,
                            "web_view_url": meta.get("webViewLink"),
                        }
                    )
            finally:
                _cleanup_temp_artifacts(cleanup_files, cleanup_dirs)

    return {"files": results}
