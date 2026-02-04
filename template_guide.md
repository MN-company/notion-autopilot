# Notion Template Guide
This guide helps you create a Notion template that pairs with the Notion Autopilot GPT. The template focuses on fast setup and clear preferences.

## Template contents
- A database named "Notion Autopilot Preferences" for global defaults.
- A "Quickstart" page with usage tips.
- Example pages that demonstrate Autopilot Overrides.

## Preferences database setup
Create a database with the following properties:
- Key (Title)
- Value (Text)
- Type (Select: string, number, boolean, enum, list, json)
- Scope (Select: global, page, workspace)
- Applies_to (Select: all, summarize, rewrite, extract, organize, meeting, coding)
- Inferred (Checkbox)
- Confidence (Number)
- Source (Text)
- Last_seen (Date)

### Suggested explicit defaults
Add these rows with Inferred unchecked:
- Key: tldr_length | Type: enum | Value: short | Applies_to: summarize | Scope: global
- Key: tone | Type: enum | Value: neutral | Applies_to: all | Scope: global
- Key: formatting_style | Type: enum | Value: clean | Applies_to: all | Scope: global
- Key: action_items_format | Type: enum | Value: to_do | Applies_to: meeting | Scope: global
- Key: section_defaults | Type: list | Value: Context, Objective, Key Points, Risks, Open Questions | Applies_to: summarize | Scope: global
- Key: change_log | Type: boolean | Value: true | Applies_to: all | Scope: global
- Key: date_format | Type: string | Value: YYYY-MM-DD | Applies_to: all | Scope: global
- Key: timezone | Type: string | Value: UTC | Applies_to: all | Scope: global

The agent can add inferred rows for safe, observable style signals (for example: heading_density, callout_usage, code_blocking, list_style) with Inferred checked and Confidence set.

## Quickstart page (recommended content)
- How to share the template and the preferences database with the Notion integration.
- How to use a page section titled "Autopilot Overrides" to set per-page preferences.
- A short example request such as "Add a TL;DR and a Next Steps checklist." 

## Autopilot Overrides example
On any page, add a heading titled "Autopilot Overrides" and write key/value pairs as plain text:
- tone: professional
- tldr_length: medium
- action_items_format: bullets

## Publishing to the Notion template gallery
- Move the template to a public workspace or a space you can share.
- Enable sharing and allow duplication.
- Ensure the preferences database is included and shared with the integration.
- Submit the template to the Notion template gallery with a short description and screenshots.
