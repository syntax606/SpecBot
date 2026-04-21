import os
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
from activity_logger import ActivityLogger
from security import (
    verify_slack_signature,
    verify_recall_signature,
    verify_confluence_signature,
    check_rate_limit,
)

app = Flask(__name__)

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
confluence = ConfluenceClient()
claude = ClaudeClient()
recall = RecallClient()


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


def resolve_user_name(user_id: str) -> str:
    try:
        info = slack_client.users_info(user=user_id)
        return info["user"]["real_name"]
    except Exception:
        return user_id


live_manager = LiveProposalManager(slack_client, post_message)
logger = ActivityLogger()
live_manager.logger = logger  # inject after both are initialised


def rate_limited() -> bool:
    """Returns True (and should reject) if the request IP is over the rate limit."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    return not check_rate_limit(ip)


# ── Spec Q&A ──────────────────────────────────────────────────────────────────

def handle_spec_question(question, channel, thread_ts=None, user=None, user_name="Unknown"):
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

    # Log the question asynchronously so it never blocks the response
    threading.Thread(
        target=logger.log_question,
        args=(user_name, user or "", question, pages)
    ).start()


# ── Slash commands ────────────────────────────────────────────────────────────

@app.route("/slack/commands", methods=["POST"])
def slash_command():
    if rate_limited():
        return jsonify({"error": "Rate limit exceeded"}), 429
    if not verify_slack_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.form
    text = data.get("text", "").strip()
    channel = data.get("channel_id")
    user = data.get("user_id")
    thread_ts = data.get("thread_ts") or None

    # /specbot live <meeting_url>
    if text.lower().startswith("live "):
        meeting_url = text[5:].strip()
        if not meeting_url.startswith("http"):
            return jsonify({"response_type": "ephemeral", "text": "Usage: `/specbot live <meeting URL>`"})

        def start_call_session():
            session_id = str(time.time())
            user_name = resolve_user_name(user)
            session = live_manager.start_session(session_id, channel)
            session.started_by = user_name
            session.session_type = "call"
            bot = recall.create_bot(meeting_url, session_id)
            session.bot_id = bot["id"]

        threading.Thread(target=start_call_session).start()
        return jsonify({"response_type": "in_channel", "text": f"<@{user}> SpecBot is joining the call and will write your proposal live..."})

    # /specbot brainstorm
    if text.lower() == "brainstorm":
        def start_thread_session():
            user_name = resolve_user_name(user)
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
            session = live_manager.start_session(session_id, channel)
            session.started_by = user_name
            session.session_type = "thread"

        threading.Thread(target=start_thread_session).start()
        return jsonify({"response_type": "in_channel", "text": f"<@{user}> Starting brainstorm thread..."})

    # /specbot done
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
                "• `/specbot brainstorm` — start a live Slack thread brainstorm\n"
                "• `/specbot live <meeting URL>` — join a call and write proposal live\n"
                "• `/specbot done` — end the active brainstorm session"
            )
        })

    # Default: spec Q&A
    threading.Thread(target=handle_spec_question, args=(text, channel, thread_ts, user, resolve_user_name(user))).start()
    return jsonify({"response_type": "in_channel", "text": f"<@{user}> asked: _{text}_"})


# ── Slack Events ──────────────────────────────────────────────────────────────

@app.route("/slack/events", methods=["POST"])
def events():
    if rate_limited():
        return jsonify({"error": "Rate limit exceeded"}), 429
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
            user_id = event.get("user", "")
            threading.Thread(target=handle_spec_question, args=(question, channel, thread_ts, user_id, resolve_user_name(user_id))).start()

    # Thread replies — feed into active brainstorm session
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        thread_ts = event.get("thread_ts")
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


# ── Confluence Webhook (direct spec edits) ────────────────────────────────────

@app.route("/confluence/webhook", methods=["POST"])
def confluence_webhook():
    if rate_limited():
        return jsonify({"error": "Rate limit exceeded"}), 429
    if not verify_confluence_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.json or {}
    event = payload.get("event", "")

    if event == "page_updated":
        page = payload.get("page", {})
        page_id = str(page.get("id", ""))
        page_title = page.get("title", "Unknown page")
        editor = payload.get("updateAuthor", {})
        editor_name = editor.get("displayName") or editor.get("name", "Unknown")

        space_key = page.get("space", {}).get("key", "")
        if space_key == os.environ.get("CONFLUENCE_SPACE_KEY", ""):
            threading.Thread(
                target=logger.log_spec_edit,
                args=(page_title, page_id, editor_name)
            ).start()

    return jsonify({"ok": True})


# ── Recall.ai Webhook ─────────────────────────────────────────────────────────

@app.route("/recall/webhook", methods=["POST"])
def recall_webhook():
    if rate_limited():
        return jsonify({"error": "Rate limit exceeded"}), 429
    if not verify_recall_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.json or {}

    chunk = recall.parse_transcript_chunk(payload)
    if chunk:
        session_id = chunk["session_id"]
        session = live_manager.get_session(session_id)
        if session and session.active:
            live_manager.add_utterance(session_id, chunk["speaker"], chunk["text"])

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
    if rate_limited():
        return jsonify({"error": "Rate limit exceeded"}), 429
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
