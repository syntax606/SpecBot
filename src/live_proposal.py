import threading
import time
from dataclasses import dataclass, field
from claude_client import ClaudeClient
from confluence_client import ConfluenceClient


# ── How often to regenerate the proposal (seconds) ───────────────────────────
AUTO_UPDATE_INTERVAL = 60   # auto-update every 60s if new content arrived
MIN_WORDS_TO_UPDATE = 30    # don't update until we have at least this many words


@dataclass
class BrainstormSession:
    session_id: str                        # Slack thread ts or custom ID
    channel_id: str                        # Slack channel to post status updates
    confluence_page_id: str = ""           # set once page is created
    confluence_page_url: str = ""
    bot_id: str = ""                       # Recall bot ID (if call-based)
    started_by: str = ""                   # display name of user who started session
    session_type: str = "thread"           # "thread" or "call"
    format_example: str = ""              # gold standard spec content for formatting
    utterances: list[dict] = field(default_factory=list)
    last_updated_at: float = 0.0
    word_count_at_last_update: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: bool = True
    # update mode — bot edits an existing spec page instead of creating a new draft
    update_mode: bool = False
    target_page_id: str = ""
    target_page_title: str = ""
    target_page_url: str = ""


class LiveProposalManager:
    def __init__(self, slack_client, post_message_fn):
        self.claude = ClaudeClient()
        self.confluence = ConfluenceClient()
        self.slack = slack_client
        self.post_message = post_message_fn   # callable(channel, text, blocks, thread_ts)
        self.logger = None  # set after init via live_manager.logger = logger
        self._sessions: dict[str, BrainstormSession] = {}
        self._start_background_updater()

    # ── Session management ────────────────────────────────────────────────────

    def start_session(self, session_id: str, channel_id: str, bot_id: str = "", target_page_title: str = "") -> BrainstormSession:
        """Create a new live session. If target_page_title is given, runs in update mode."""
        import os
        session = BrainstormSession(
            session_id=session_id,
            channel_id=channel_id,
            bot_id=bot_id,
        )
        self._sessions[session_id] = session

        if target_page_title:
            pages = self.confluence.search_by_title(target_page_title, limit=1)
            if pages:
                session.update_mode = True
                session.target_page_id = pages[0]["id"]
                session.target_page_title = pages[0]["title"]
                session.target_page_url = self.confluence.page_url(pages[0]["id"])
                self.post_message(
                    channel_id,
                    f"🔴 *Live spec update started!* I'm listening and will patch "
                    f"<{session.target_page_url}|{session.target_page_title}> as the team makes decisions.\n\n"
                    f"_Type `done` or `/specbot done` to end the session._",
                    thread_ts=session_id,
                )
                return session
            else:
                # Page not found — warn and fall through to create mode
                self.post_message(
                    channel_id,
                    f"⚠️ Couldn't find a spec page matching *{target_page_title}* — starting in brainstorm mode instead.",
                    thread_ts=session_id,
                )

        # Create mode: fetch gold standard format example and create a new draft page
        session.format_example = self.confluence.get_gold_standard_spec()
        if session.format_example:
            print(f"Gold standard spec loaded ({len(session.format_example)} chars)")
        else:
            print("No gold standard spec configured — using default proposal format")

        space_key = os.environ.get("CONFLUENCE_SPACE_KEY", "")
        page_url = self.confluence.create_draft_page(
            title="🔴 LIVE — Feature Brainstorm (updating...)",
            content="*This proposal is being written live. Check back soon.*",
            space_key=space_key,
        )
        page_id = page_url.rstrip("/").split("/")[-1]
        session.confluence_page_id = page_id
        session.confluence_page_url = page_url

        self.post_message(
            channel_id,
            f"🔴 *Live brainstorm started!* I'm writing the proposal as you talk.\n"
            f"📄 <{page_url}|Watch it update in Confluence>\n\n"
            f"_To stop the session and finalise, type `done` or use `/specbot done`_",
            thread_ts=session_id,
        )
        return session

    def add_utterance(self, session_id: str, speaker: str, text: str, trigger_update: bool = False):
        """Add a transcript chunk to the session buffer."""
        session = self._sessions.get(session_id)
        if not session or not session.active:
            return

        with session.lock:
            session.utterances.append({"speaker": speaker, "text": text})

        if trigger_update:
            self._update_proposal(session)

    def end_session(self, session_id: str) -> str:
        """Finalise the session — do a last update then clean up."""
        session = self._sessions.get(session_id)
        if not session:
            return ""

        session.active = False
        self._update_proposal(session, final=True)

        if session.update_mode:
            url = session.target_page_url
            self.post_message(
                session.channel_id,
                f"✅ *Spec update complete!* All changes have been applied.\n📄 <{url}|Open spec>",
                thread_ts=session_id,
            )
            if self.logger:
                threading.Thread(
                    target=self.logger.log_spec_edit,
                    args=(session.target_page_title, session.target_page_id, session.started_by)
                ).start()
        else:
            url = session.confluence_page_url
            self.post_message(
                session.channel_id,
                f"✅ *Brainstorm complete!* Final proposal saved to Confluence.\n📄 <{url}|Open proposal>",
                thread_ts=session_id,
            )
            if self.logger:
                threading.Thread(
                    target=self.logger.log_brainstorm,
                    args=(session.started_by, session.session_type, url, "Feature Proposal (Draft)")
                ).start()

        del self._sessions[session_id]
        return url

    def get_session(self, session_id: str) -> BrainstormSession | None:
        return self._sessions.get(session_id)

    def get_session_by_bot(self, bot_id: str) -> BrainstormSession | None:
        for s in self._sessions.values():
            if s.bot_id == bot_id:
                return s
        return None

    # ── Proposal generation ───────────────────────────────────────────────────

    def _build_transcript_text(self, session: BrainstormSession) -> str:
        lines = []
        for u in session.utterances:
            lines.append(f"{u['speaker']}: {u['text']}")
        return "\n".join(lines)

    def _current_word_count(self, session: BrainstormSession) -> int:
        return sum(len(u["text"].split()) for u in session.utterances)

    def _update_proposal(self, session: BrainstormSession, final: bool = False):
        """Route to the right update strategy based on session mode."""
        if session.update_mode:
            self._update_existing_page(session, final=final)
            return
        self._regenerate_proposal(session, final=final)

    def _update_existing_page(self, session: BrainstormSession, final: bool = False):
        """Update mode: ask Claude which sections changed, patch them in-memory, one PUT."""
        with session.lock:
            word_count = self._current_word_count(session)
            if word_count < MIN_WORDS_TO_UPDATE and not final:
                return
            if word_count == session.word_count_at_last_update and not final:
                return
            transcript = self._build_transcript_text(session)
            session.word_count_at_last_update = word_count
            session.last_updated_at = time.time()

        try:
            page_content = self.confluence.get_page_content(session.target_page_id)
            raw_html = self.confluence.get_page_raw_html(session.target_page_id)
        except Exception as e:
            print(f"update_existing_page: failed to fetch page {session.target_page_id}: {e}")
            return

        try:
            changes = self.claude.identify_section_changes(transcript, page_content)
        except Exception as e:
            print(f"update_existing_page: Claude failed: {e}")
            return

        if not changes:
            return

        # Apply all patches to in-memory HTML so we do one PUT regardless of section count
        updated_html = raw_html
        applied = []
        for change in changes:
            heading = change.get("section_heading", "").strip()
            revised = change.get("revised_section", "")
            summary = change.get("summary", "")
            if not heading or not revised:
                continue
            section_data = self.confluence.extract_section(updated_html, heading)
            if not section_data:
                print(f"update_existing_page: section '{heading}' not found in HTML, skipping")
                continue
            new_html = self.confluence._markdown_to_confluence(revised)
            updated_html = updated_html.replace(section_data["full_html"], new_html, 1)
            applied.append(f"• *{heading}*: {summary}")

        if not applied:
            return

        self.confluence.update_page(
            page_id=session.target_page_id,
            title=session.target_page_title,
            content=updated_html,
            raw_html=True,
        )

        if final:
            return

        bullet_list = "\n".join(applied)
        self.post_message(
            session.channel_id,
            f"_📝 Spec patched — <{session.target_page_url}|{session.target_page_title}>_\n{bullet_list}",
            thread_ts=session.session_id,
        )

    def _regenerate_proposal(self, session: BrainstormSession, final: bool = False):
        """Create mode: regenerate the full proposal draft and push to Confluence."""
        with session.lock:
            word_count = self._current_word_count(session)
            if word_count < MIN_WORDS_TO_UPDATE and not final:
                return
            if word_count == session.word_count_at_last_update and not final:
                return
            transcript = self._build_transcript_text(session)
            session.word_count_at_last_update = word_count
            session.last_updated_at = time.time()

        proposal = self.claude.draft_proposal(transcript, format_example=session.format_example)

        title = "Feature Proposal (Draft)" if final else "🔴 LIVE — Feature Brainstorm (updating...)"
        self.confluence.update_page(
            page_id=session.confluence_page_id,
            title=title,
            content=proposal,
        )

        if final:
            return  # end_session() handles the final message
        self.post_message(
            session.channel_id,
            f"_📝 Proposal updated — <{session.confluence_page_url}|view in Confluence>_",
            thread_ts=session.session_id,
        )

    # ── Background auto-updater ───────────────────────────────────────────────

    def _start_background_updater(self):
        def loop():
            while True:
                time.sleep(10)
                for session in list(self._sessions.values()):
                    if not session.active:
                        continue
                    elapsed = time.time() - session.last_updated_at
                    if elapsed >= AUTO_UPDATE_INTERVAL:
                        self._update_proposal(session)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
