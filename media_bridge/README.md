# Media Bridge (Optional Middleware)

GPT Actions can send files to your API as short-lived download URLs (`openaiFileIdRefs`), but most third-party APIs (Notion uploads, Google Drive uploads) require raw bytes via multipart uploads.

This small middleware service:
- Downloads files from `openaiFileIdRefs[*].download_link`
- Auto-expands slide decks:
  - `pdf` -> auto-detect and crop diagram regions (fallback: one PNG per page)
  - `ppt/pptx` -> convert to PDF (if LibreOffice is installed) -> one PNG per slide
- Uploads them to Notion (Direct Upload) and/or Google Drive
- Returns IDs/URLs the GPT can embed into Notion pages

## Endpoints
- `POST /v1/notion/file_uploads` uploads files to Notion and returns `file_upload_id`s.
- `POST /v1/drive/upload_public` uploads files to Drive, makes them public, and returns public URLs.

## Configuration
Environment variables:
- `NOTION_TOKEN` (optional): Notion integration token (`ntn_...`) for single-workspace deployments. If not set, the service expects a per-user Notion token in `Authorization: Bearer ...`.
- `NOTION_VERSION` (optional): defaults to `2022-06-28`
- `ALLOWED_DOWNLOAD_HOSTS` (optional): comma-separated allowlist for `openaiFileIdRefs[*].download_link` hosts.
- `ALLOWED_DOWNLOAD_HOST_SUFFIXES` (optional): comma-separated allowlist for host suffixes (e.g. `oaiusercontent.com`) to accept dynamic subdomains.
- `MAX_DOWNLOAD_BYTES` (optional): maximum file size the bridge will download (default: 30 MiB).
- `MAX_SLIDE_PAGES` (optional): max pages/slides rendered from a deck (default: 80).
- `SLIDE_RENDER_DPI` (optional): PNG render DPI for slide images (default: 150).
- `PDF_EXTRACT_MODE` (optional): `diagram` (default) to crop likely diagram regions, or `page` to export full pages only.

Google Drive:
- The service expects a Google OAuth access token in the request `Authorization: Bearer ...` header.
- Configure OAuth in your GPT Action using Google as the provider and scope `https://www.googleapis.com/auth/drive.file`.

## Local run
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

## Deploy
Any HTTPS host works (Cloud Run, Fly.io, Render, etc.). The GPT Action requires a public HTTPS URL with a valid certificate.

If you want automatic `ppt/pptx` conversion on the bridge runtime, install LibreOffice in the container image (`soffice` must be available on PATH).
