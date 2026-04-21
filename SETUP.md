# SpecBot — Setup Guide

## What you're building
A Slack bot that:
1. Answers engineers' questions about feature specs (sourced from Confluence)
2. Turns brainstorm transcripts into structured proposals, then publishes them to Confluence

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

---

## Step 2 — Deploy to Railway

Railway gives you a free tier and a public URL without needing to manage servers.

1. Go to https://railway.app and sign up (free)
2. Click **New Project → Deploy from GitHub repo**
3. Push this code to a GitHub repo first (or use Railway's CLI)
4. Once deployed, Railway gives you a URL like `https://specbot-production.up.railway.app`
5. Go to **Variables** in your Railway project and add all values from `.env.example`

> 💡 Your `PORT` variable is set automatically by Railway — you don't need to add it.

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

## Step 4 — Set up the brainstorm channel

1. Create a Slack channel called `#feature-brainstorm` (or whatever you like)
2. Invite SpecBot to it: `/invite @SpecBot`
3. Get the channel ID: Right-click the channel → **View channel details** → scroll to the bottom — it shows the Channel ID (starts with `C`)
4. Add this as `BRAINSTORM_CHANNEL_ID` in Railway Variables

---

## Step 5 — Test it

### Test Spec Q&A
In any Slack channel where SpecBot is invited:
```
/specbot What is the authentication flow for the login feature?
```
Or mention it:
```
@SpecBot What does the spec say about error handling?
```

### Test Brainstorm → Proposal
Go to your brainstorm channel and paste:
```
TRANSCRIPT: [paste your Google Meet transcript here]
```
SpecBot will reply with a draft proposal and buttons to publish or revise.

---

## How to use it day-to-day

| Scenario | What to do |
|---|---|
| Engineer has a spec question | `/specbot [question]` in any channel |
| @-mention mid-conversation | `@SpecBot [question]` in a thread |
| After a brainstorm call | Download Meet transcript → paste into `#feature-brainstorm` with `TRANSCRIPT:` prefix |
| Approve proposal | Click **Publish to Confluence** button |
| Want changes | Click **Revise** and reply in the thread |

---

## Costs (approximate)

| Service | Cost |
|---|---|
| Railway | Free tier (500 hours/month) or $5/month Pro |
| Anthropic API | ~$0.01–0.05 per question at this scale |
| Slack | Free (uses existing workspace) |
| Confluence | Free (uses existing licence) |

**Realistic monthly cost for a team of <10: under £10.**

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

**Railway deployment failing**
- Check the build logs
- Make sure all required environment variables are set
