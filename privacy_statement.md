# Privacy Statement
This project provides configuration files for a GPT-style Notion agent. It does not include a hosted backend. When you deploy or run it, the data handling depends on your environment.

## What data is accessed
- The agent accesses Notion content only after you connect a Notion integration and explicitly share pages or databases with it.
- It uses that content solely to fulfill the task you requested.
- If you connect Google Drive, the agent can access files permitted by the OAuth scopes you grant (for example, files created or selected by the app).
- If you enable OpenID scopes (openid/email/profile), the agent may read basic profile data such as name and email for personalization or attribution.

## What we do not do
- We do not sell or rent user data.
- We do not attempt to access content that has not been shared with the integration.
- We do not request or store your Google password.
- We do not store user profile data in Notion unless you explicitly ask.

## Your responsibilities when self-hosting
If you run this yourself, you control retention, logging, and access controls. Review your hosting provider and Notion integration settings to align with your privacy requirements.
