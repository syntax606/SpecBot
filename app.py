import os
import hmac
import hashlib
import time
import threading
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from confluence_client import ConfluenceClient
from claude_client import ClaudeClient

app = Flask(__name__)

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
confluence = ConfluenceClient()
claude = ClaudeClient()

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
BRAINSTORM_CHANNEL = os.environ.get("BRAINSTORM_CHANNEL_ID", "")


def verify_slack_signature(request):
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{request.get_data(as_text=True)}"
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    slack_signature = request.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(my_signature, slack_signature)


def post_message(channel, text, blocks=None, thread_ts=None):
    kwargs = {"channel": channel, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        slack_client.chat_postMessage(**kwargs)
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}")


# ── Spec Q&A ──────────────────────────────────────────────────────────────────

def handle_spec_question(question, channel, thread_ts=None, user=None):
    """Fetch relevant spec from Confluence and answer the question via Claude."""
    # Let the user know we're working
    post_message(channel, f"_Searching specs for: {question}..._", thread_ts=thread_ts)

    # Search Confluence for relevant pages
    pages = confluence.search(question, limit=3)
    if not pages:
        post_message(channel, "I couldn't find any relevant specs for that question. Try linking a specific Confluence page.", thread_ts=thread_ts)
        return

    # Build context from top pages
    context_parts = []
    for page in pages:
        content = confluence.get_page_content(page["id"])
        context_parts.append(f"## {page['title']}\n\n{content}")
    spec_context = "\n\n---\n\n".join(context_parts)

    # Ask Claude
    answer = claude.answer_spec_question(question, spec_context)

    # Format response with source links
    source_links = "\n".join([f"• <{confluence.page_url(p['id'])}|{p['title']}>" for p in pages])
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Answer:*\n{answer}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sources:*\n{source_links}"}},
    ]
    post_message(channel, answer, blocks=blocks, thread_ts=thread_ts)


# ── Brainstorm → Proposal ─────────────────────────────────────────────────────

def handle_brainstorm_transcript(transcript, channel, thread_ts=None):
    """Turn a pasted brainstorm transcript into a structured proposal."""
    post_message(channel, "_Reading the brainstorm transcript and drafting a proposal..._", thread_ts=thread_ts)

    proposal = claude.draft_proposal(transcript)

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*📋 Proposal Draft*\nReview below, then publish to Confluence when ready."},
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": proposal[:2900]}},  # Slack block limit
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": "proposal_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Publish to Confluence"},
                    "style": "primary",
                    "action_id": "publish_proposal",
                    "value": proposal[:2000],  # Store proposal in value (trimmed for safety)
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ Revise"},
                    "action_id": "revise_proposal",
                    "value": "revise",
                },
            ],
        },
    ]
    post_message(channel, "Proposal draft ready.", blocks=blocks, thread_ts=thread_ts)


# ── Slack Routes ──────────────────────────────────────────────────────────────

@app.route("/slack/commands", methods=["POST"])
def slash_command():
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.form
    question = data.get("text", "").strip()
    channel = data.get("channel_id")
    user = data.get("user_id")

    if not question:
        return jsonify({"response_type": "ephemeral", "text": "Usage: `/specbot <your question about the spec>`"})

    # Respond immediately (Slack requires <3s), handle async
    threading.Thread(target=handle_spec_question, args=(question, channel, None, user)).start()
    return jsonify({"response_type": "in_channel", "text": f"<@{user}> asked: _{question}_"})


@app.route("/slack/events", methods=["POST"])
def events():
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.json

    # Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_type = event.get("type")

    # Handle @mentions
    if event_type == "app_mention":
        text = event.get("text", "")
        # Strip the bot mention
        question = " ".join(w for w in text.split() if not w.startswith("<@")).strip()
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        if question:
            threading.Thread(target=handle_spec_question, args=(question, channel, thread_ts)).start()

    # Handle messages in brainstorm channel
    if event_type == "message" and event.get("channel") == BRAINSTORM_CHANNEL:
        text = event.get("text", "")
        # Detect transcript pastes: look for trigger keyword or length
        if text.startswith("TRANSCRIPT:") or len(text) > 500:
            channel = event.get("channel")
            thread_ts = event.get("ts")
            transcript = text.replace("TRANSCRIPT:", "").strip()
            threading.Thread(target=handle_brainstorm_transcript, args=(transcript, channel, thread_ts)).start()

    return jsonify({"ok": True})


@app.route("/slack/interactions", methods=["POST"])
def interactions():
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    import json
    payload = json.loads(request.form["payload"])
    actions = payload.get("actions", [])
    channel = payload["channel"]["id"]
    thread_ts = payload["message"].get("thread_ts") or payload["message"].get("ts")
    user = payload["user"]["id"]

    for action in actions:
        action_id = action.get("action_id")

        if action_id == "publish_proposal":
            proposal_text = action.get("value", "")
            post_message(channel, "_Publishing to Confluence..._", thread_ts=thread_ts)

            def publish():
                page_url = confluence.create_draft_page(
                    title="Feature Proposal (Draft)",
                    content=proposal_text,
                    space_key=os.environ.get("CONFLUENCE_SPACE_KEY", "")
                )
                post_message(
                    channel,
                    f"✅ Published! Review and rename it here: {page_url}",
                    thread_ts=thread_ts
                )
            threading.Thread(target=publish).start()

        elif action_id == "revise_proposal":
            post_message(
                channel,
                f"<@{user}> Reply in this thread with your changes and I'll revise the proposal.",
                thread_ts=thread_ts
            )

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 3000)))
