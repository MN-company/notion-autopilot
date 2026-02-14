from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlencode, urlparse
from pathlib import Path

import cv2
import httpx
import fitz  # PyMuPDF
import numpy as np
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
PDF_EXTRACT_MODE = (_env("PDF_EXTRACT_MODE", "diagram") or "diagram").strip().lower()
NOTION_OAUTH_AUTHORIZE_URL = _env("NOTION_OAUTH_AUTHORIZE_URL", "https://api.notion.com/v1/oauth/authorize")
NOTION_OAUTH_TOKEN_URL = _env("NOTION_OAUTH_TOKEN_URL", "https://api.notion.com/v1/oauth/token")


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


def _clamp(v: int, low: int, high: int) -> int:
    return max(low, min(v, high))


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(1, (ax1 - ax0) * (ay1 - ay0))
    b_area = max(1, (bx1 - bx0) * (by1 - by0))
    return inter / float(a_area + b_area - inter)


def _extract_diagrams_from_pdf(pdf_path: str, source_name: str) -> list[dict[str, str]]:
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
        output: list[dict[str, str]] = []

        for page_idx in range(doc.page_count):
            page = doc.load_page(page_idx)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            h, w, n = pix.height, pix.width, pix.n
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, n)
            if n == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
            elif n == 1:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)

            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            mask = (gray < 245).astype(np.uint8) * 255

            # Remove text areas from candidate mask.
            page_w = max(1.0, float(page.rect.width))
            page_h = max(1.0, float(page.rect.height))
            sx = w / page_w
            sy = h / page_h
            blocks = (page.get_text("dict") or {}).get("blocks", [])
            for block in blocks:
                if block.get("type") != 0:
                    continue
                x0, y0, x1, y1 = block.get("bbox", (0, 0, 0, 0))
                px0 = _clamp(int(x0 * sx) - 8, 0, w)
                py0 = _clamp(int(y0 * sy) - 8, 0, h)
                px1 = _clamp(int(x1 * sx) + 8, 0, w)
                py1 = _clamp(int(y1 * sy) + 8, 0, h)
                mask[py0:py1, px0:px1] = 0

            kernel = np.ones((5, 5), np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            page_area = float(w * h)
            boxes: list[tuple[int, int, int, int]] = []
            for c in contours:
                x, y, bw, bh = cv2.boundingRect(c)
                area = float(bw * bh)
                ratio = area / page_area
                if ratio < 0.02 or ratio > 0.90:
                    continue
                if bw < 140 or bh < 100:
                    continue
                # Ignore header/footer stripes.
                if y < int(0.04 * h) or (y + bh) > int(0.98 * h):
                    continue
                pad_x = max(8, int(0.01 * w))
                pad_y = max(8, int(0.01 * h))
                x0 = _clamp(x - pad_x, 0, w)
                y0 = _clamp(y - pad_y, 0, h)
                x1 = _clamp(x + bw + pad_x, 0, w)
                y1 = _clamp(y + bh + pad_y, 0, h)
                boxes.append((x0, y0, x1, y1))

            # Keep non-overlapping largest regions.
            boxes = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
            kept: list[tuple[int, int, int, int]] = []
            for b in boxes:
                if any(_iou(b, k) > 0.45 for k in kept):
                    continue
                kept.append(b)
                if len(kept) >= 6:
                    break

            for idx, (x0, y0, x1, y1) in enumerate(kept):
                crop = img[y0:y1, x0:x1]
                if crop.size == 0:
                    continue
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as out:
                    out_path = out.name
                cv2.imwrite(out_path, cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
                output.append(
                    {
                        "path": out_path,
                        "name": f"{base}_p{page_idx + 1:03d}_diagram_{idx + 1:02d}.png",
                        "mime_type": "image/png",
                    }
                )

        return output
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
        else:
            images = _extract_diagrams_from_pdf(file_path, name)
            if not images:
                # Fallback when no diagram regions are detected.
                images = _render_pdf_to_pngs(file_path, name)
        cleanup_files.extend([img["path"] for img in images])
        return images, cleanup_files, cleanup_dirs

    if _is_presentation(name, mime_type):
        pdf_path, tmp_dir = _convert_presentation_to_pdf(file_path)
        cleanup_dirs.append(tmp_dir)
        images = _render_pdf_to_pngs(pdf_path, name)
        cleanup_files.extend([img["path"] for img in images])
        return images, cleanup_files, cleanup_dirs

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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/oauth/notion/authorize")
async def oauth_notion_authorize(request: Request) -> RedirectResponse:
    params = dict(request.query_params)
    params.setdefault("owner", "user")
    params.setdefault("response_type", "code")
    redirect_to = f"{NOTION_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
    return RedirectResponse(url=redirect_to, status_code=302)


@app.post("/oauth/notion/token")
async def oauth_notion_token(request: Request) -> Response:
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    forward_headers = {"Content-Type": content_type}
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

                    # Step 1: create upload object
                    create_resp = await client.post(
                        "https://api.notion.com/v1/file_uploads",
                        headers={
                            "Authorization": f"Bearer {notion_token}",
                            "Notion-Version": NOTION_VERSION,
                            "Content-Type": "application/json",
                        },
                        json={"mode": "single_part", "filename": upload_name, "content_type": upload_mime},
                    )
                    if create_resp.status_code >= 400:
                        raise HTTPException(status_code=create_resp.status_code, detail=create_resp.text)
                    upload_obj = create_resp.json()
                    upload_id = upload_obj.get("id")
                    if not upload_id:
                        raise HTTPException(status_code=502, detail="Notion did not return a file upload id.")

                    # Step 2: send bytes
                    with open(upload_path, "rb") as fp:
                        send_resp = await client.post(
                            f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
                            headers={
                                "Authorization": f"Bearer {notion_token}",
                                "Notion-Version": NOTION_VERSION,
                            },
                            files={"file": (upload_name, fp, upload_mime)},
                        )
                    if send_resp.status_code >= 400:
                        # Bubble up: caller can fallback to Drive if this is a size-limit error.
                        raise HTTPException(status_code=send_resp.status_code, detail=send_resp.text)

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
