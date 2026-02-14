from __future__ import annotations

import json
import os
import tempfile
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException

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
MAX_DOWNLOAD_BYTES = int(_env("MAX_DOWNLOAD_BYTES", str(30 * 1024 * 1024)) or str(30 * 1024 * 1024))


def _validate_download_link(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="download_link must use https.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="download_link is missing a hostname.")
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"download_link host '{host}' is not allowed. Set ALLOWED_DOWNLOAD_HOSTS to include it.",
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


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def _notion_token_from_request(authorization: str | None) -> str:
    # Prefer per-user token via Authorization header (works with OAuth-configured GPT Actions).
    token = _extract_bearer(authorization)
    if token:
        return token
    # Fallback to server-configured integration token (single workspace deployments).
    if NOTION_TOKEN:
        return NOTION_TOKEN
    raise HTTPException(
        status_code=401,
        detail="Missing Notion token. Provide Authorization: Bearer <notion_token> or configure NOTION_TOKEN on the service.",
    )


def _openai_files_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    refs = body.get("openaiFileIdRefs") or []
    if not isinstance(refs, list):
        raise HTTPException(status_code=400, detail="openaiFileIdRefs must be an array.")
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

            # Step 1: create upload object
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

            # Step 2: send bytes
            with open(tmp_path, "rb") as fp:
                send_resp = await client.post(
                    f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
                    headers={
                        "Authorization": f"Bearer {notion_token}",
                        "Notion-Version": NOTION_VERSION,
                    },
                    files={"file": (name, fp, mime_type)},
                )
            if send_resp.status_code >= 400:
                # Bubble up: caller can fallback to Drive if this is a size-limit error.
                raise HTTPException(status_code=send_resp.status_code, detail=send_resp.text)

            results.append(
                {
                    "name": name,
                    "mime_type": mime_type,
                    "file_upload_id": upload_id,
                }
            )

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
            with open(tmp_path, "rb") as fp:
                file_bytes = fp.read()

            boundary = "notion-autopilot-boundary"
            metadata = {"name": name, "parents": [folder_id]}
            body_bytes = (
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\n"
                f"Content-Type: {mime_type}\r\n\r\n"
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
                    "name": name,
                    "mime_type": mime_type,
                    "file_id": file_id,
                    "public_url": public_url,
                    "web_view_url": meta.get("webViewLink"),
                }
            )

    return {"files": results}
