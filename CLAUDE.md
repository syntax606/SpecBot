# SpecBot — Claude Code Instructions

## What this project is
A Slack bot for a small software development team that:
- Answers engineer questions grounded in Confluence spec pages
- Runs live AI-assisted brainstorm sessions via Slack threads
- Joins Google Meet calls via Recall.ai to write proposals in real time
- Logs all spec activity to a master Confluence audit page

## Stack
- Python / Flask backend, hosted on Railway
- Slack (slash commands, @mentions, thread messages)
- Anthropic Claude API (claude-sonnet-4-20250514)
- Confluence REST API (read, write, update pages)
- Recall.ai (meeting bot + real-time transcription)

## Project structure
- src/app.py — all Flask routes and Slack event handling
- src/claude_client.py — Claude prompts for Q&A and proposal drafting
- src/confluence_client.py — all Confluence API calls
- src/recall_client.py — Recall.ai bot lifecycle and webhook parsing
- src/live_proposal.py — live session manager, buffers transcript, updates Confluence
- src/activity_logger.py — appends to master Spec Activity Log in Confluence
- src/security.py — HMAC verification for all webhooks, rate limiting

## Key conventions
- All webhook verification lives in security.py — don't inline auth logic in routes
- Logging is always async (threading.Thread) so it never blocks responses
- Gold standard spec formatting is fetched once per session in live_proposal.py
- Never hardcode credentials — all secrets come from environment variables

## Running locally
- Copy .env.example to .env and fill in real values
- pip install -r requirements.txt
- python src/app.py

## Deployment
- Hosted on Railway, auto-deploys from GitHub main branch
- Environment variables set in Railway dashboard, never committed to git
- railway.toml configures the start command and health check

## Things to be careful about
- The Confluence API token belongs to a service account, not a personal account
- Rate limiting is in security.py — 30 requests per IP per 60 seconds
- Slack requires a response within 3 seconds — long operations run in threads
