# SpecBot — Setup Guide

## What you're building
A Slack bot that:
1. Answers engineers' questions about feature specs (sourced from Confluence)
2. Runs live brainstorm sessions in Slack threads or Google Meet calls, writing proposals to Confluence in real time

---

## Step 1 — Get your API keys

### Anthropic (Claude)
1. Go to https://console.anthropic.com
2. Create an account and add a payment method
3. Go to API Keys → Create Key
4. Copy it — this is your `ANTHROPIC_API_KEY`

### Confluence
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click **Create API token** → name it "SpecBot"
3. Copy the token — this is your `CONFLUENCE_API_TOKEN`
4. Your `CONFLUENCE_BASE_URL` is: `https://yourcompany.atlassian.net/wiki`
5. Your `CONFLUENCE_EMAIL` is the email you log into Atlassian with
6. Your `CONFLUENCE_SPACE_KEY`: Go to your specs space in Confluence → look at the URL, it'll say `/spaces/ENG` or similar — that's your space key

### Recall.ai
1. Go to https://www.recall.ai and create an account
2. From the dashboard, copy your **API Key** — this is your `RECALL_API_KEY`
3. Note your region (e.g. `us-east-1`) — this is your `RECALL_REGION`
4. Your `RECALL_WEBHOOK_URL` will be `https://YOUR_DOMAIN/recall/webhook` — you'll fill in the domain after Step 2

---

## Step 2 — Deploy to Railway

Railway gives you a free tier and a public URL without needing to manage servers.

1. Go to https://railway.app and sign up (free)
2. Click **New Project → Deploy from GitHub repo**
3. Push this code to a GitHub repo first (or use Railway's CLI)
4. Once deployed, Railway gives you a URL like `https://specbot-production.up.railway.app`
5. Go to **Variables** in your Railway project and add all values from `.env.example`

> 💡 Your `PORT` variable is set automatically by Railway — you don't need to add it.

> 💡 Now that you have your Railway URL, go back and set `RECALL_WEBHOOK_URL` to `https://specbot-production.up.railway.app/recall/webhook`

---

## Step 3 — Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App → From manifest**
2. Select your workspace
3. Paste the contents of `config/slack_manifest.yaml`
4. Replace `YOUR_DOMAIN` with your Railway URL (e.g. `https://specbot-production.up.railway.app`)
5. Click **Create**

### Get your Slack tokens
- Go to **OAuth & Permissions → Install to Workspace**
- Copy the **Bot User OAuth Token** → this is your `SLACK_BOT_TOKEN`
- Go to **Basic Information → App Credentials** → copy **Signing Secret** → this is your `SLACK_SIGNING_SECRET`

Add both to Railway Variables.

---

## Step 4 — Test it

### Test Spec Q&A
In any Slack channel where SpecBot is invited:
```
/specbot What is the authentication flow for the login feature?
```
Or mention it directly:
```
@SpecBot What does the spec say about error handling?
```

### Test Slack thread brainstorm
In any channel:
```
/specbot brainstorm
```
SpecBot will start a thread and create a live Confluence draft. Reply in the thread as if you're talking through the feature. The proposal updates every 60 seconds. When done:
```
/specbot done
```

### Test live call brainstorm
In any channel:
```
/specbot live https://meet.google.com/abc-defg-hij
```
SpecBot joins the call via Recall.ai and writes the proposal live as the conversation happens. It finalises automatically when the call ends.

---

## How to use it day-to-day

| Scenario | What to do |
|---|---|
| Engineer has a spec question | `/specbot [question]` in any channel |
| @-mention mid-conversation | `@SpecBot [question]` in a thread |
| Start a Slack brainstorm | `/specbot brainstorm` → talk in the thread → `done` when finished |
| Start a live call brainstorm | `/specbot live [meeting URL]` before or during the call |
| Force a proposal refresh mid-session | Reply `update` in the brainstorm thread |
| End a session early | Reply `done` in the thread or `/specbot done` |

---

## Costs (approximate)

| Service | Cost |
|---|---|
| Railway | Free tier (500 hours/month) or $5/month Pro |
| Anthropic API | ~$0.01–0.05 per question at this scale |
| Recall.ai | ~$0.15/hour of call time |
| Slack | Free (uses existing workspace) |
| Confluence | Free (uses existing licence) |

**Realistic monthly cost for a team of <10: under £15.**

---

## Troubleshooting

**Bot doesn't respond to /specbot**
- Check Railway logs for errors
- Verify `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` are set correctly

**"I couldn't find any relevant specs"**
- Check `CONFLUENCE_SPACE_KEY` matches your actual space
- Make sure the Confluence API token has read access to that space

**Proposal not publishing to Confluence**
- Check `CONFLUENCE_BASE_URL` ends with `/wiki` (not just the domain)
- Verify the API token has page creation permissions

**SpecBot doesn't join the call**
- Verify `RECALL_API_KEY` and `RECALL_REGION` are set correctly
- Make sure `RECALL_WEBHOOK_URL` points to your live Railway URL (not localhost)

**Railway deployment failing**
- Check the build logs
- Make sure all required environment variables are set
