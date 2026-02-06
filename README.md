# Notion Autopilot
Notion Autopilot is a community-oriented GPT configuration that helps operate Notion via the API. This repo shares the prompt, OpenAPI schema, and starter prompts so you can run and improve the GPT.

The internal codebase that powers the private version (v4) is not published yet. The next release (v5) is planned to open-source the implementation.

## What's in this repo
- `prompt.md`: The full system prompt used by the GPT.
- `notion_api.yaml`: A strict, parser-friendly OpenAPI schema for the Notion API.
- `conversation_starters.md`: Sample prompts to help users get started.
- `privacy_statement.md`: A plain-language privacy statement template.
- `seed_preferences.csv`: Starter rows for the preferences database.

## Status
- Current internal version: v4
- Next public release: v5 (planned)

## Contributing
PRs and issue reports are welcome. If you have ideas for better prompts, improved API schemas, or additional conversation starters, feel free to open a pull request.

## Preferences CSV
Use `seed_preferences.csv` to quickly populate the "Notion Autopilot Preferences" database. Import it into Notion and then edit values as needed, including creative controls such as `creative_level`, `layout_style`, and `visual_weight`.

## Dynamic Config Page
Optionally create a page titled "Autopilot Config" to define runtime rules and sync preferences. The agent reads this page at the start of each conversation and applies its rules for the session.
