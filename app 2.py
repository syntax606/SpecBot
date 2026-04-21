import os
import hmac
import hashlib
import time
import threading
import json
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from confluence_client import ConfluenceClient
from claude_client import ClaudeClient
from recall_client import RecallClient
from live_proposal import LiveProposalManager

app = Flask(__name__)

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
confluence = ConfluenceClient()
claude = ClaudeClient()
recall = RecallClient()

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]


# ── Helpers ───────────────────────────────────────────────────────────────────

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


live_manager = LiveProposalManager(slack_client, post_message)


def verify_slack_signature(req):
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    slack_signature = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(my_signature, slack_signature)


# ── Spec Q&A ──────────────────────────────────────────────────────────────────

def handle_spec_question(question, channel, thread_ts=None, user=None):
    post_message(channel, f"_Searching specs for: {question}..._", thread_ts=thread_ts)
    pages = confluence.search(question, limit=3)
    if not pages:
        post_message(channel, "Couldn't find relevant specs. Try a different question or link a specific page.", thread_ts=thread_ts)
        return

    context_parts = []
    for page in pages:
        content = confluence.get_page_content(page["id"])
        context_parts.append(f"## {page['title']}\n\n{content}")
    spec_context = "\n\n---\n\n".join(context_parts)
    answer = claude.answer_spec_question(question, spec_context)

    source_links = "\n".join([f"• <{confluence.page_url(p['id'])}|{p['title']}>" for p in pages])
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Answer:*\n{answer}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Sources:*\n{source_links}"}},
    ]
    post_message(channel, answer, blocks=blocks, thread_ts=thread_ts)


# ── Slash commands ────────────────────────────────────────────────────────────

@app.route("/slack/commands", methods=["POST"])
def slash_command():
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.form
    text = data.get("text", "").strip()
    channel = data.get("channel_id")
    user = data.get("user_id")
    thread_ts = data.get("thread_ts") or None

    # /specbot live <meeting_url>  — Option A: join call via Recall
    if text.lower().startswith("live "):
        meeting_url = text[5:].strip()
        if not meeting_url.startswith("http"):
            return jsonify({"response_type": "ephemeral", "text": "Usage: `/specbot live <meeting URL>`"})

        def start_call_session():
            session_id = str(time.time())
            session = live_manager.start_session(session_id, channel)
            bot = recall.create_bot(meeting_url, session_id)
            session.bot_id = bot["id"]

        threading.Thread(target=start_call_session).start()
        return jsonify({"response_type": "in_channel", "text": f"<@{user}> SpecBot is joining the call and will write your proposal live..."})

    # /specbot brainstorm  — Option C: live Slack thread session
    if text.lower() == "brainstorm":
        def start_thread_session():
            result = slack_client.chat_postMessage(
                channel=channel,
                text=(
                    f"<@{user}> started a live brainstorm. Talk freely in this thread — "
                    f"I'll write the proposal as you go.\n\n"
                    f"*Commands (reply in this thread):*\n"
                    f"• `update` — force a proposal refresh now\n"
                    f"• `done` — finalise and publish to Confluence"
                )
            )
            session_id = result["ts"]
            live_manager.start_session(session_id, channel)

        threading.Thread(target=start_thread_session).start()
        return jsonify({"response_type": "in_channel", "text": f"<@{user}> Starting brainstorm thread..."})

    # /specbot done  — end active session
    if text.lower() == "done":
        session = next((s for s in live_manager._sessions.values() if s.channel_id == channel), None)
        if not session:
            return jsonify({"response_type": "ephemeral", "text": "No active brainstorm session in this channel."})

        def finish():
            if session.bot_id:
                try:
                    recall.remove_bot(session.bot_id)
                except Exception:
                    pass
            live_manager.end_session(session.session_id)

        threading.Thread(target=finish).start()
        return jsonify({"response_type": "in_channel", "text": "Finalising your proposal..."})

    # /specbot (no args) — help
    if not text:
        return jsonify({
            "response_type": "ephemeral",
            "text": (
                "*SpecBot commands:*\n"
                "• `/specbot <question>` — ask about a spec\n"
                "• `/specbot brainstorm` — start a live Slack thread brainstorm (Option C)\n"
                "• `/specbot live <meeting URL>` — join a call and write proposal live (Option A)\n"
                "• `/specbot done` — end the active brainstorm session"
            )
        })

    # Default: spec Q&A
    threading.Thread(target=handle_spec_question, args=(text, channel, thread_ts, user)).start()
    return jsonify({"response_type": "in_channel", "text": f"<@{user}> asked: _{text}_"})


# ── Slack Events ──────────────────────────────────────────────────────────────

@app.route("/slack/events", methods=["POST"])
def events():
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.json

    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_type = event.get("type")

    # @SpecBot mention — spec Q&A
    if event_type == "app_mention":
        text = event.get("text", "")
        question = " ".join(w for w in text.split() if not w.startswith("<@")).strip()
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        if question:
            threading.Thread(target=handle_spec_question, args=(question, channel, thread_ts)).start()

    # Thread replies — feed into active brainstorm session (Option C)
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        thread_ts = event.get("thread_ts")  # only present if message is a thread reply
        channel = event.get("channel")
        user = event.get("user", "Someone")

        if thread_ts:
            session = live_manager.get_session(thread_ts)
            if session and session.active:
                if text.lower() == "update":
                    threading.Thread(target=live_manager._update_proposal, args=(session,)).start()
                    post_message(channel, "_Refreshing proposal now..._", thread_ts=thread_ts)
                elif text.lower() == "done":
                    def finish_thread():
                        if session.bot_id:
                            try:
                                recall.remove_bot(session.bot_id)
                            except Exception:
                                pass
                        live_manager.end_session(thread_ts)
                    threading.Thread(target=finish_thread).start()
                else:
                    try:
                        info = slack_client.users_info(user=user)
                        name = info["user"]["real_name"]
                    except Exception:
                        name = "Team"
                    live_manager.add_utterance(thread_ts, name, text)

    return jsonify({"ok": True})


# ── Recall.ai Webhook ─────────────────────────────────────────────────────────

@app.route("/recall/webhook", methods=["POST"])
def recall_webhook():
    """Recall streams transcript chunks here in real time during the call."""
    payload = request.json or {}

    # Real-time transcript chunk
    chunk = recall.parse_transcript_chunk(payload)
    if chunk:
        session_id = chunk["session_id"]
        session = live_manager.get_session(session_id)
        if session and session.active:
            live_manager.add_utterance(session_id, chunk["speaker"], chunk["text"])

    # Call ended — auto-finalise
    if payload.get("event") == "bot.status_change":
        status_code = payload.get("data", {}).get("status", {}).get("code", "")
        if status_code in ("call_ended", "done"):
            bot_id = payload.get("bot_id", "")
            session = live_manager.get_session_by_bot(bot_id)
            if session:
                threading.Thread(target=live_manager.end_session, args=(session.session_id,)).start()

    return jsonify({"ok": True})


# ── Slack Interactions ────────────────────────────────────────────────────────

@app.route("/slack/interactions", methods=["POST"])
def interactions():
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

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
                post_message(channel, f"✅ Published! <{page_url}|Open in Confluence>", thread_ts=thread_ts)
            threading.Thread(target=publish).start()

        elif action_id == "revise_proposal":
            post_message(channel, f"<@{user}> Reply here with your changes and I'll revise.", thread_ts=thread_ts)

    return jsonify({"ok": True})


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active_sessions": len(live_manager._sessions)})


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 3000)))
