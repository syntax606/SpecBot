import os
import time
import threading
import json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
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

_pending_edits: dict = {}  # edit_key -> edit state dict


def parse_time_range(phrase: str) -> tuple[datetime, datetime]:
    """Parse natural language time phrases into (since, until) UTC datetimes."""
    now = datetime.now(timezone.utc)
    p = phrase.strip().lower()

    if p == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if p == "yesterday":
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today - timedelta(days=1), today
    if p in ("last week", "this week"):
        return now - timedelta(days=7), now

    import re as _re
    m = _re.match(r"last (\d+) days?", p)
    if m:
        return now - timedelta(days=int(m.group(1))), now

    m = _re.match(r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", p)
    if m:
        since = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        until = datetime.fromisoformat(m.group(2)).replace(tzinfo=timezone.utc, hour=23, minute=59, second=59)
        return since, until

    return now - timedelta(days=7), now  # default: last 7 days


def rate_limited() -> bool:
    """Returns True (and should reject) if the request IP is over the rate limit."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    return not check_rate_limit(ip)


# ── Spec Q&A ──────────────────────────────────────────────────────────────────

def handle_spec_changelog(page_query: str, time_phrase: str, channel: str, thread_ts, user_name: str):
    pages = confluence.search_by_title(page_query, limit=3)
    if not pages:
        pages = confluence.search(page_query, limit=3)
    if not pages:
        post_message(channel, f"Couldn't find a spec page matching *{page_query}*.", thread_ts=thread_ts)
        return

    page = pages[0]
    page_id = page["id"]
    page_title = page["title"]
    page_url = confluence.page_url(page_id)

    since, until = parse_time_range(time_phrase)

    post_message(channel, f"_Fetching change history for *{page_title}*..._", thread_ts=thread_ts)

    versions = confluence.get_page_versions(page_id, since, until)
    if not versions:
        since_str = since.strftime("%b %d")
        until_str = until.strftime("%b %d")
        post_message(channel, f"No changes to <{page_url}|{page_title}> found between {since_str} and {until_str}.", thread_ts=thread_ts)
        return

    # Compare the version just before the range (or v1) against current
    oldest_in_range = versions[0]["number"]
    compare_from = max(1, oldest_in_range - 1)
    old_content = confluence.get_page_content_at_version(page_id, compare_from)
    current_content = confluence.get_page_content(page_id)

    summary = claude.summarize_spec_changes(old_content, current_content, page_title, versions)

    since_str = since.strftime("%b %d")
    until_str = until.strftime("%b %d")
    date_range = f"{since_str} – {until_str}" if since_str != until_str else since_str
    editors = ", ".join(sorted({v["by"] for v in versions}))
    edit_count = len(versions)

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Changelog: <{page_url}|{page_title}>*\n_{date_range} · {edit_count} edit{'s' if edit_count != 1 else ''} · {editors}_"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]
    post_message(channel, f"Changelog for {page_title}", blocks=blocks, thread_ts=thread_ts)


def handle_spec_edit(page_query: str, section: str, instruction: str, channel: str, thread_ts, user: str, user_name: str):
    pages = confluence.search_by_title(page_query, limit=3)
    if not pages:
        pages = confluence.search(page_query, limit=3)
    if not pages:
        post_message(channel, f"Couldn't find a spec page matching *{page_query}*. Try a more specific title.", thread_ts=thread_ts)
        return

    page = pages[0]
    page_id = page["id"]
    page_title = page["title"]
    page_content = confluence.get_page_content(page_id)
    raw_html = confluence.get_page_raw_html(page_id)

    post_message(channel, f"_Found *{page_title}*. Drafting edit..._", thread_ts=thread_ts)

    try:
        result = claude.draft_section_edit(page_content, section, instruction)
    except Exception as e:
        post_message(channel, f"Couldn't draft the edit: {e}", thread_ts=thread_ts)
        return

    section_heading = result.get("section_heading", "")
    revised_section = result.get("revised_section", "")
    summary = result.get("summary", "")

    section_data = confluence.extract_section(raw_html, section_heading)
    if not section_data:
        post_message(channel, f"Couldn't locate the *{section_heading}* section in the page. Try specifying the section name more precisely.", thread_ts=thread_ts)
        return

    new_section_html = confluence._markdown_to_confluence(revised_section)
    edit_key = f"{channel}:{user}:{int(time.time())}"
    _pending_edits[edit_key] = {
        "page_id": page_id,
        "page_title": page_title,
        "page_url": confluence.page_url(page_id),
        "section_heading": section_heading,
        "old_html": section_data["full_html"],
        "new_html": new_section_html,
        "original_instruction": instruction,
        "page_content": page_content,
        "section": section,
        "channel": channel,
        "thread_ts": thread_ts,
        "user": user,
        "user_name": user_name,
        "awaiting_revision": False,
        "revision_thread_ts": None,
    }

    preview = revised_section[:600] + ("..." if len(revised_section) > 600 else "")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Proposed edit to `{section_heading}` in <{confluence.page_url(page_id)}|{page_title}>*\n_{summary}_"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{preview}```"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "action_id": "approve_edit", "value": edit_key},
            {"type": "button", "text": {"type": "plain_text", "text": "✏️ Revise"}, "action_id": "revise_edit", "value": edit_key},
            {"type": "button", "text": {"type": "plain_text", "text": "🗑️ Discard"}, "style": "danger", "action_id": "discard_edit", "value": edit_key},
        ]},
    ]
    post_message(channel, f"Proposed edit to {page_title}", blocks=blocks, thread_ts=thread_ts)


def handle_spec_question(question, channel, thread_ts=None, user=None, user_name="Unknown"):
    try:
        # Prefer the configured gold standard page; fall back to keyword search
        gold_id = os.environ.get("CONFLUENCE_GOLD_STANDARD_PAGE_ID", "")
        if gold_id:
            pages = [{"id": gold_id, "title": "Spec"}]
        else:
            pages = confluence.search(question, limit=3)
            if not pages:
                pages = confluence.list_all_pages(limit=5)

        if pages:
            context_parts = []
            for page in pages:
                try:
                    content = confluence.get_page_content(page["id"])
                    context_parts.append(f"## {page['title']}\n\n{content}")
                except Exception:
                    pass
            if context_parts:
                spec_context = "\n\n---\n\n".join(context_parts)
                answer = claude.answer_spec_question(question, spec_context)
                post_message(channel, answer, thread_ts=thread_ts)
                threading.Thread(
                    target=logger.log_question,
                    args=(user_name, user or "", question, pages)
                ).start()
                return

        # No spec pages available — answer from general Claude knowledge
        answer = claude.answer_general(question)
        post_message(channel, answer, thread_ts=thread_ts)
        threading.Thread(
            target=logger.log_question,
            args=(user_name, user or "", question, [])
        ).start()

    except Exception as e:
        print(f"handle_spec_question error: {e}")
        post_message(channel, f"⚠️ Something went wrong: `{e}`", thread_ts=thread_ts)


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

    # /specbot log <page title> | last 7 days
    if text.lower().startswith("log "):
        parts = [p.strip() for p in text[4:].split("|", 1)]
        page_query = parts[0]
        time_phrase = parts[1] if len(parts) == 2 else "last 7 days"
        if not page_query:
            return jsonify({"response_type": "ephemeral", "text": "Usage: `/specbot log <page title> | last 7 days`"})
        threading.Thread(target=handle_spec_changelog, args=(page_query, time_phrase, channel, thread_ts, resolve_user_name(user))).start()
        return jsonify({"response_type": "in_channel", "text": f"<@{user}> Checking the changelog..."})

    # /specbot edit <page title> | <section> | <instruction>
    # /specbot edit <page title> | <instruction>
    if text.lower().startswith("edit "):
        parts = [p.strip() for p in text[5:].split("|")]
        if len(parts) == 3:
            page_query, section, instruction = parts
        elif len(parts) == 2:
            page_query, instruction = parts
            section = "auto"
        else:
            return jsonify({"response_type": "ephemeral", "text": "Usage: `/specbot edit <page title> | <instruction>` or `/specbot edit <page title> | <section name> | <instruction>`"})

        user_name = resolve_user_name(user)
        threading.Thread(target=handle_spec_edit, args=(page_query, section, instruction, channel, thread_ts, user, user_name)).start()
        return jsonify({"response_type": "in_channel", "text": f"<@{user}> Looking up the spec page..."})

    # /specbot live <meeting_url>
    # /specbot live <meeting_url> | <existing page title>
    if text.lower().startswith("live "):
        live_args = text[5:].strip()
        if "|" in live_args:
            meeting_url, target_page_title = [p.strip() for p in live_args.split("|", 1)]
        else:
            meeting_url, target_page_title = live_args, ""

        if not meeting_url.startswith("http"):
            return jsonify({"response_type": "ephemeral", "text": "Usage: `/specbot live <meeting URL>` or `/specbot live <meeting URL> | <existing page title>`"})

        def start_call_session():
            session_id = str(time.time())
            user_name = resolve_user_name(user)
            session = live_manager.start_session(session_id, channel, target_page_title=target_page_title)
            session.started_by = user_name
            session.session_type = "call"
            bot = recall.create_bot(meeting_url, session_id)
            session.bot_id = bot["id"]

        threading.Thread(target=start_call_session).start()
        if target_page_title:
            reply = f"<@{user}> SpecBot is joining the call and will update *{target_page_title}* live..."
        else:
            reply = f"<@{user}> SpecBot is joining the call and will write your proposal live..."
        return jsonify({"response_type": "in_channel", "text": reply})

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
                "• `/specbot log <page title> | last 7 days` — show what changed in a spec (supports: today, yesterday, last N days, last week, YYYY-MM-DD to YYYY-MM-DD)\n"
                "• `/specbot edit <page title> | <instruction>` — edit a spec section\n"
                "• `/specbot edit <page title> | <section name> | <instruction>` — edit a specific section\n"
                "• `/specbot brainstorm` — start a live Slack thread brainstorm\n"
                "• `/specbot live <meeting URL>` — join a call and write a new proposal live\n"
                "• `/specbot live <meeting URL> | <page title>` — join a call and update an existing spec live\n"
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
            else:
                # Check if this reply is a revision instruction for a pending edit
                for key, edit in list(_pending_edits.items()):
                    if edit.get("awaiting_revision") and edit.get("revision_thread_ts") == thread_ts and edit.get("channel") == channel:
                        edit["awaiting_revision"] = False

                        def redo_edit(e=edit, k=key, revision=text):
                            try:
                                result = claude.draft_section_edit(e["page_content"], e["section_heading"], revision)
                                revised_section = result.get("revised_section", "")
                                summary = result.get("summary", "")
                                e["new_html"] = confluence._markdown_to_confluence(revised_section)
                                preview = revised_section[:600] + ("..." if len(revised_section) > 600 else "")
                                blocks = [
                                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Revised edit to `{e['section_heading']}` in <{e['page_url']}|{e['page_title']}>*\n_{summary}_"}},
                                    {"type": "section", "text": {"type": "mrkdwn", "text": f"```{preview}```"}},
                                    {"type": "actions", "elements": [
                                        {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "action_id": "approve_edit", "value": k},
                                        {"type": "button", "text": {"type": "plain_text", "text": "✏️ Revise"}, "action_id": "revise_edit", "value": k},
                                        {"type": "button", "text": {"type": "plain_text", "text": "🗑️ Discard"}, "style": "danger", "action_id": "discard_edit", "value": k},
                                    ]},
                                ]
                                post_message(e["channel"], f"Revised edit to {e['page_title']}", blocks=blocks, thread_ts=e["thread_ts"])
                            except Exception as ex:
                                post_message(e["channel"], f"Revision failed: {ex}", thread_ts=e["thread_ts"])

                        threading.Thread(target=redo_edit).start()
                        break

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

        if action_id == "approve_edit":
            edit_key = action.get("value", "")
            edit = _pending_edits.get(edit_key)
            if not edit:
                post_message(channel, "Edit expired or already applied.", thread_ts=thread_ts)
                continue

            def apply_edit(e=edit, key=edit_key):
                try:
                    page_url = confluence.replace_section_html(e["page_id"], e["page_title"], e["old_html"], e["new_html"])
                    post_message(e["channel"], f"✅ *Edit applied!* <{page_url}|View updated spec>", thread_ts=e["thread_ts"])
                    threading.Thread(target=logger.log_spec_edit, args=(e["page_title"], e["page_id"], e["user_name"])).start()
                except Exception as ex:
                    post_message(e["channel"], f"Failed to apply edit: {ex}", thread_ts=e["thread_ts"])
                finally:
                    _pending_edits.pop(key, None)

            threading.Thread(target=apply_edit).start()

        elif action_id == "revise_edit":
            edit_key = action.get("value", "")
            edit = _pending_edits.get(edit_key)
            if not edit:
                post_message(channel, "Edit expired or not found.", thread_ts=thread_ts)
                continue
            edit["awaiting_revision"] = True
            edit["revision_thread_ts"] = thread_ts
            post_message(channel, "_Reply here with your revision instructions and I'll redo the edit._", thread_ts=thread_ts)

        elif action_id == "discard_edit":
            edit_key = action.get("value", "")
            _pending_edits.pop(edit_key, None)
            post_message(channel, "_Edit discarded._", thread_ts=thread_ts)

        elif action_id == "publish_proposal":
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
