# Notion Autopilot
Notion Autopilot is a GPT configuration for people who live in Notion and want an agent that actually does the work: find the right page, read it fully, restructure it, and ship clean edits with minimal back-and-forth.

The goal is simple: fewer "where is the page?" questions, more finished pages.

## Highlights
- Attempt-first, ask-last behavior: it searches, picks the right page, and executes.
- Macro redesigns: when you say "redo my home/dashboard", it proposes layouts and applies one decisively.
- Style-aware editing: a lightweight preferences database plus a `Note` field for observed style (for example, "H1 -> yellow").
- Slide-to-Notion: extract slides to images and insert them into Notion.
- Media that just works: direct Notion uploads when possible, Google Drive external fallback when Notion limits block uploads.

## What's in this repo
- `prompt.md`: The full system prompt used by the GPT.
- `notion_api.yaml`: A strict, parser-friendly OpenAPI schema for the Notion API.
- `google_drive_api.yaml`: Minimal Google Drive API schema for media fallback uploads.
- `media_bridge/`: Optional middleware for autonomous slide/image uploads (Notion + Drive).
- `conversation_starters.md`: Sample prompts to help users get started.
- `privacy_statement.md`: Privacy policy aligned with the prompt and schemas.
- `seed_preferences.csv`: Starter rows for the preferences database.

## Status
- Current internal version (code): v4 (not published)
- V5 focus (this repo): prompt + schemas + preferences model

## Quickstart
- Create a Notion integration and share the pages/databases you want the agent to access.
- Create a database named "Notion Autopilot Preferences" with properties: `Key` (title), `Value` (rich_text), `Note` (rich_text).
- Import `seed_preferences.csv` into that database and set `workspace_plan` to `free` or `paid`.
- (Recommended) Deploy the Media Bridge service in `media_bridge/` and add it as an Action so the GPT can upload files autonomously.
- (Optional) Configure a direct Google Drive Action using `google_drive_api.yaml` if you do not need file uploads from the conversation.

## Contributing
PRs and issue reports are welcome. If you have ideas for better prompts, improved API schemas, or additional conversation starters, feel free to open a pull request.

## Preferences CSV
Use `seed_preferences.csv` to quickly populate the preferences database, then tweak values to match your workflow. The `Note` field is meant for the GPT to record observed style signals (for example: "H1 -> yellow", "callouts rare").

## Google Drive fallback
When images exceed Notion workspace limits (common on free workspaces), the prompt uses Google Drive as a fallback:
- Upload to a dedicated folder named "Notion Autopilot Media".
- Create a public permission and attach the image as an external URL in Notion.

Use `google_drive_api.yaml` as the action schema for Drive API calls.
