# SpecBot

A Slack bot that answers engineer questions from your Confluence specs, runs live AI-assisted brainstorm sessions, edits existing spec pages section by section, and logs all spec activity to a master audit trail.

Built with Claude (Anthropic), Recall.ai, Confluence, and Slack. Deployable on Railway in under an hour.

---

## What it does

### 1. Spec Q&A
Engineers ask questions in Slack. SpecBot searches your Confluence spec pages, passes the relevant content to Claude, and returns a grounded answer with source links — no hallucination, no invention beyond what the spec says.

```
/specbot What is the error handling flow for the login feature?
@SpecBot Does the spec cover offline mode?
```

### 2. Live brainstorm → proposal (Slack thread)
Start a live session in Slack. Talk through the feature idea in the thread naturally. SpecBot builds a structured proposal in Confluence as you go, updating it every 60 seconds. When you're done, one command finalises and publishes it.

```
/specbot brainstorm
```

### 3. Live brainstorm → proposal (Google Meet)
SpecBot joins your Google Meet via Recall.ai, listens to the conversation in real time, and writes the proposal directly to a Confluence draft page as you talk. When the call ends, the proposal is finalised automatically.

```
/specbot live https://meet.google.com/abc-defg-hij
```

### 4. Live spec update (Google Meet)
Point SpecBot at an existing spec page when joining a call. Instead of creating a new proposal, it monitors the conversation and patches only the sections where the team makes a concrete decision — directly updating the live spec page every 60 seconds.

```
/specbot live https://meet.google.com/abc-defg-hij | Payments v2 Spec
```

SpecBot will tell you in Slack exactly which sections it changed and why, e.g.:
> 📝 Spec patched — Payments v2 Spec
> • *Key Requirements*: Added PCI-DSS compliance requirement following Alice's confirmation
> • *Out of Scope*: Removed 3DS2 after team agreed to defer it

### 5. Section-specific spec editing
Edit a specific section of any existing Confluence spec page directly from Slack. Claude drafts the change and shows you a preview first — you approve, revise with new instructions, or discard before anything touches Confluence.

```
/specbot edit Payments v2 Spec | Key Requirements | Add a requirement for idempotent payment retries
/specbot edit Payments v2 Spec | Remove all references to the legacy API
```

Omitting the section name lets Claude decide which section the instruction most naturally applies to.

### 6. Spec activity log
Every spec-related action is recorded to a master "Spec Activity Log" Confluence page — who asked what, who ran a brainstorm, who edited a spec page directly, and when. Newest entries appear first.

---

## Architecture

```
Slack (slash commands / @mentions / thread messages)
        ↓
Flask backend (Railway)
        ↓                          ↓
Confluence API              Recall.ai API
(search + read specs)       (bot joins Google Meet)
        ↓                          ↓
    Claude API (Anthropic) ← transcript chunks
        ↓
Confluence API
(update live draft page)
        ↓
Activity Logger
(append to Spec Activity Log page)
```

### File structure

```
SpecBot/
├── src/
│   ├── __init__.py
│   ├── app.py                # Flask app — all Slack and webhook routes
│   ├── claude_client.py      # Claude API — spec Q&A and proposal prompts
│   ├── confluence_client.py  # Confluence REST API — search, read, write, update
│   ├── recall_client.py      # Recall.ai REST API — bot lifecycle and webhook parsing
│   ├── live_proposal.py      # Live session manager — buffers transcript, drives updates
│   ├── activity_logger.py    # Master audit log — appends to Confluence log page
│   └── security.py           # Webhook signature verification and rate limiting
├── config/
│   └── slack_manifest.yaml   # Paste into Slack to create the app
├── .env.example              # All required environment variables
├── .gitignore
├── CLAUDE.md
├── requirements.txt
├── railway.toml              # Railway deployment config
├── SETUP.md                  # Step-by-step setup guide
└── README.md
```

---

## Slack commands

| Command | Description |
|---|---|
| `/specbot <question>` | Answer a question from the spec |
| `/specbot edit <page> \| <instruction>` | Edit the most relevant section of a spec page |
| `/specbot edit <page> \| <section> \| <instruction>` | Edit a specific named section |
| `/specbot brainstorm` | Start a live Slack thread brainstorm session |
| `/specbot live <url>` | Join a Google Meet and write a new proposal live |
| `/specbot live <url> \| <page title>` | Join a Google Meet and update an existing spec live |
| `/specbot done` | End the active brainstorm or update session |
| `/specbot` | Show help |
| `update` *(in brainstorm thread)* | Force an immediate proposal refresh |
| `done` *(in brainstorm thread)* | Finalise from the thread |
| `@SpecBot <question>` | Ask a spec question mid-conversation |

---

## What gets logged

The Spec Activity Log Confluence page records:

| Event | Trigger | Recorded |
|---|---|---|
| ❓ Spec Question | Any `/specbot` or `@SpecBot` query | User name, question text, specs searched |
| 🧠 Brainstorm | Session ends via `done` | Who started it, mode (call/thread), link to proposal |
| ✏️ Spec Edit | `/specbot edit` approval, live update session ending, or direct Confluence page edit | Editor name, page title, link |

Direct Confluence spec edits require a Confluence webhook to be configured — see SETUP.md. Edits made via SpecBot are logged automatically without a webhook.

---

## Tech stack

| Component | Technology |
|---|---|
| Bot framework | Python / Flask |
| AI | Claude Sonnet (Anthropic API) |
| Live call transcription | Recall.ai |
| Spec storage | Confluence (Atlassian REST API) |
| Slack integration | slack-sdk |
| Hosting | Railway |

---

## Environment variables

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token from Slack app settings |
| `SLACK_SIGNING_SECRET` | Signing secret from Slack app Basic Information |
| `ANTHROPIC_API_KEY` | Claude API key from console.anthropic.com |
| `CONFLUENCE_BASE_URL` | e.g. `https://yourcompany.atlassian.net/wiki` |
| `CONFLUENCE_EMAIL` | Atlassian account email |
| `CONFLUENCE_API_TOKEN` | API token from id.atlassian.com |
| `CONFLUENCE_SPACE_KEY` | Key of your specs space, e.g. `ENG` |
| `RECALL_API_KEY` | API key from recall.ai dashboard |
| `RECALL_REGION` | Your Recall region, e.g. `us-east-1` |
| `RECALL_WEBHOOK_URL` | `https://YOUR_DOMAIN/recall/webhook` |
| `CONFLUENCE_GOLD_STANDARD_PAGE_ID` | Optional — ID of your best spec page, used as a formatting template for proposals |
| `CONFLUENCE_WEBHOOK_SECRET` | Optional — shared secret to verify Confluence webhook requests |
| `RECALL_WEBHOOK_SECRET` | Optional — shared secret to verify Recall.ai webhook requests |

---

## Approximate running costs

| Service | Cost |
|---|---|
| Railway | Free tier / ~£4/month Pro |
| Anthropic API | ~£0.01–0.05 per session at this scale |
| Recall.ai | ~£0.52/hour of call time |
| Slack + Confluence | Existing plan — no extra cost |

**Estimated monthly total for a team under 10: under £15.**

---

## Setup

See [SETUP.md](SETUP.md) for the full step-by-step guide covering API keys, Railway deployment, Slack app creation, and Confluence webhook configuration.

---

## Notes

- Spec content is sent to the Anthropic API to generate answers. Anthropic's paid API plans include a zero data retention policy — inputs are not stored or used for training.
- Recall.ai receives meeting audio to produce transcripts. Review their [data processing terms](https://www.recall.ai) if this is a concern for your organisation.
- The bot answers spec questions strictly from Confluence content. If a spec doesn't cover a question, it says so explicitly rather than guessing.
