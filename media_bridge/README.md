# Media Bridge (Optional Middleware)

GPT Actions can send files to your API as short-lived download URLs (`openaiFileIdRefs`), but most third-party APIs (Notion uploads, Google Drive uploads) require raw bytes via multipart uploads.

This small middleware service:
- Downloads files from `openaiFileIdRefs[*].download_link`
- Optionally expands slide decks only when explicitly enabled (`PDF_EXTRACT_MODE=page`)
- Uploads them to Notion (Direct Upload) and/or Google Drive
- Returns IDs/URLs the GPT can embed into Notion pages

## Endpoints
- `POST /v1/notion/file_uploads` uploads files to Notion and returns `file_upload_id`s.
- `POST /v1/notion/file_uploads_from_data` uploads base64 file bytes to Notion (for sandbox-generated files) and returns `file_upload_id`s.
- `POST /v1/drive/upload_public` uploads files to Drive, makes them public, and returns public URLs.
- `GET /oauth/notion/authorize` OAuth authorize proxy for Notion (same-domain URL for GPT Actions).
- `POST /oauth/notion/token` OAuth token proxy for Notion (same-domain URL for GPT Actions).

## Configuration
Environment variables:
- `NOTION_TOKEN` (optional): Notion integration token (`ntn_...`) for single-workspace deployments. If not set, the service expects a per-user Notion token in `Authorization: Bearer ...`.
- `NOTION_VERSION` (optional): defaults to `2022-06-28`
- `ALLOWED_DOWNLOAD_HOSTS` (optional): comma-separated allowlist for `openaiFileIdRefs[*].download_link` hosts.
- `ALLOWED_DOWNLOAD_HOST_SUFFIXES` (optional): comma-separated allowlist for host suffixes (e.g. `oaiusercontent.com`) to accept dynamic subdomains.
- `MAX_DOWNLOAD_BYTES` (optional): maximum file size the bridge will download (default: 30 MiB).
- `MAX_SLIDE_PAGES` (optional): max pages/slides rendered from a deck (default: 80).
- `SLIDE_RENDER_DPI` (optional): PNG render DPI for slide images (default: 150).
- `PDF_EXTRACT_MODE` (optional): `none` (default) for pure upload behavior; `page` to convert PDF/PPTX to page PNGs.
- `NOTION_OAUTH_AUTHORIZE_URL` (optional): defaults to `https://api.notion.com/v1/oauth/authorize`
- `NOTION_OAUTH_TOKEN_URL` (optional): defaults to `https://api.notion.com/v1/oauth/token`
- `NOTION_OAUTH_CLIENT_ID` (optional): if set, bridge forces this client id during OAuth authorize.
- `NOTION_OAUTH_CLIENT_SECRET` (optional): if set with client id, bridge signs token exchange with these credentials.
- `MAX_INLINE_FILE_BYTES` (optional): max bytes for each base64 inline file upload (default: 8 MiB).

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

## Multi-user Notion OAuth setup (recommended for GPT Store)
Use this when each user must connect their own Notion workspace.

1. In Notion integrations, create a public OAuth integration and copy `client_id` + `client_secret`.
2. In GPT Actions, set Authentication to OAuth and configure:
   - Authorization URL: `https://<YOUR_RUN_APP>/oauth/notion/authorize`
   - Token URL: `https://<YOUR_RUN_APP>/oauth/notion/token`
   - Scope: leave empty (Notion does not use OAuth scopes in the same way as Google Drive).
3. In Notion integration settings, add the redirect URI shown by GPT Actions.
4. Do not set `NOTION_TOKEN` on Cloud Run for multi-user mode.
5. Recommended: set `NOTION_OAUTH_CLIENT_ID` and `NOTION_OAUTH_CLIENT_SECRET` on Cloud Run.
6. The bridge will then normalize OAuth token exchange (form-to-JSON + Basic auth) and use each user's Notion bearer token at runtime.

## Sandbox file upload path
When a file exists only in sandbox/code output (not as `openaiFileIdRefs`), call:

`POST /v1/notion/file_uploads_from_data`

with:
- `files[].name`
- `files[].mime_type`
- `files[].data_base64`

The bridge decodes bytes, applies deck extraction for PDF/PPTX when applicable, and uploads to Notion.
