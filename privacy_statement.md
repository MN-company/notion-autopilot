# Privacy Policy (Notion Autopilot GPT)
Last updated: 2026-02-14

This GPT helps you edit and organize your Notion content, and (optionally) upload media to Google Drive when Notion file limits prevent direct uploads. For fully autonomous file handling (for example, extracting images from slide decks and uploading them), this repo includes an optional middleware service (`media_bridge/`) that downloads short-lived file URLs from the conversation and performs the required multipart uploads to Notion/Google Drive. It is designed to use the minimum access required and avoid collecting personal identity data.

## What data is accessed
- Notion content you choose to make available to the Notion integration (pages, databases, and blocks the integration can access).
- Files you explicitly provide to the GPT (for example, slide decks or images), only for the purpose of extracting and inserting images into Notion.
- If you connect Google Drive: the specific files and folders created or uploaded by the GPT as part of the fallback flow.

## What the GPT does with your data
- Reads Notion content to understand your request and the page(s) to edit.
- Writes updates back to Notion (blocks/pages) to complete the task.
- When needed, converts slides into images and inserts those images into Notion.
- If Notion upload fails due to workspace limits, uploads the image to a dedicated Drive folder (for example, \"Notion Autopilot Media\") and inserts a link to that externally hosted image in Notion.

## What the GPT does not do
- It does not request Google OpenID scopes (`openid`, `email`, `profile`) and does not fetch your name or email from Google.
- It does not attempt to access Notion content that is not shared with the integration.
- It does not sell or rent your data.

## Where data goes (third parties)
To perform tasks, the GPT may send data to:
- Notion (to read and edit your pages/databases).
- Google Drive (only if you connect it and only for the fallback upload flow).
- OpenAI/ChatGPT (the platform running the GPT). Your use of the GPT is also governed by OpenAI's own policies.
- Media Bridge (optional, if you deploy it): receives short-lived download URLs for files you provided and uploads those files to Notion/Drive to complete the task.

## Storage and retention
- This project does not operate its own external server for storing your data.
- If you deploy the optional Media Bridge service, you control where it runs. It is intended to process files transiently and not store them beyond the uploads you requested.
- Content is stored in Notion and/or Google Drive as part of the outputs you requested (for example, inserted images or updated pages).
- Any additional retention (for example, platform logs) is determined by the providers above.

## Your choices and controls
- You can revoke access at any time by disconnecting the Notion integration and/or removing the Google Drive connection.
- You can delete files uploaded to Google Drive and remove external image links from Notion.
- You control which Notion pages/databases are shared with the integration.

## Security
- Use a dedicated Notion integration and limit what it can access.
- If you enable the Google Drive fallback, prefer the least-privilege scope (`drive.file`).

## Contact
For privacy questions or deletion requests related to this GPT configuration, contact the repository owner/maintainer.
