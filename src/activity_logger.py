"""
activity_logger.py

Appends structured entries to a single master "Spec Activity Log" Confluence page.
Three event types:
  - spec_question   : who asked what, which spec pages were searched, when
  - brainstorm      : who started a session, which proposal was produced, when
  - spec_edit       : a Confluence page in the specs space was directly edited

Log page format (Confluence storage):
  A table with columns: Timestamp | Type | Who | Detail | Link
  New entries are prepended (newest first).
"""

import os
import threading
from datetime import datetime, timezone
from confluence_client import ConfluenceClient


LOG_PAGE_TITLE = "Spec Activity Log"
_lock = threading.Lock()


class ActivityLogger:
    def __init__(self):
        self.confluence = ConfluenceClient()
        self._page_id: str = ""
        try:
            self._ensure_log_page()
        except Exception as e:
            print(f"ActivityLogger: could not connect to Confluence on startup ({e}). "
                  f"Will retry on first log event.")

    def _lazy_ensure_log_page(self):
        """Call before any log write if page_id not yet set."""
        if not self._page_id:
            try:
                self._ensure_log_page()
            except Exception as e:
                print(f"ActivityLogger: Confluence still unreachable ({e})")

    # ── Public log methods ────────────────────────────────────────────────────

    def log_question(self, user_name: str, user_id: str, question: str, spec_pages: list[dict]):
        """Log a spec Q&A query."""
        sources = ", ".join(p["title"] for p in spec_pages) if spec_pages else "No matching specs"
        detail = f"Asked: <em>{self._escape(question)}</em><br/>Specs searched: {self._escape(sources)}"
        self._append_row(
            event_type="❓ Spec Question",
            who=user_name,
            detail=detail,
            link="",
        )

    def log_brainstorm(self, user_name: str, session_type: str, proposal_url: str, proposal_title: str = ""):
        """Log the start/end of a brainstorm session."""
        mode = "Live call (Recall.ai)" if session_type == "call" else "Slack thread"
        detail = f"Mode: {mode}<br/>Proposal: <a href='{proposal_url}'>{self._escape(proposal_title or 'View proposal')}</a>"
        self._append_row(
            event_type="🧠 Brainstorm",
            who=user_name,
            detail=detail,
            link=proposal_url,
        )

    def log_spec_edit(self, page_title: str, page_id: str, editor_name: str):
        """Log a direct edit to a spec page in Confluence."""
        page_url = self.confluence.page_url(page_id)
        detail = f"Edited spec: <a href='{page_url}'>{self._escape(page_title)}</a>"
        self._append_row(
            event_type="✏️ Spec Edit",
            who=editor_name,
            detail=detail,
            link=page_url,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _ensure_log_page(self):
        """Find or create the master log page."""
        space_key = os.environ.get("CONFLUENCE_SPACE_KEY", "")
        results = self.confluence.search(LOG_PAGE_TITLE, limit=1)
        # Check if any result is an exact title match
        for r in results:
            if r["title"] == LOG_PAGE_TITLE:
                self._page_id = r["id"]
                return

        # Create it fresh with table headers
        initial_content = self._build_table([])
        url = self.confluence.create_draft_page(
            title=LOG_PAGE_TITLE,
            content=initial_content,
            space_key=space_key,
        )
        # Publish it immediately (not a draft)
        self._page_id = url.rstrip("/").split("/")[-1]
        self.confluence.publish_page(self._page_id)

    def _append_row(self, event_type: str, who: str, detail: str, link: str):
        """Thread-safe: prepend a new row to the log table."""
        self._lazy_ensure_log_page()
        if not self._page_id:
            print(f"ActivityLogger: skipping log entry — Confluence page not available")
            return
        with _lock:
            # Fetch current page body
            current_html = self.confluence.get_page_raw_html(self._page_id)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            new_row = f"""
<tr>
  <td>{timestamp}</td>
  <td>{event_type}</td>
  <td>{self._escape(who)}</td>
  <td>{detail}</td>
</tr>"""

            # Insert after the header row
            updated_html = current_html.replace(
                "</tr>\n</thead>",
                "</tr>\n</thead>\n<tbody>" if "<tbody>" not in current_html else "</tr>",
            )

            # Prepend new row into tbody
            if "<tbody>" in updated_html:
                updated_html = updated_html.replace("<tbody>", f"<tbody>{new_row}", 1)
            else:
                # Fallback: just append
                updated_html += f"<table><tbody>{new_row}</tbody></table>"

            self.confluence.update_page(
                page_id=self._page_id,
                title=LOG_PAGE_TITLE,
                content=updated_html,
                raw_html=True,
            )

    def _build_table(self, rows: list[str]) -> str:
        body = "\n".join(rows)
        return f"""<table>
<thead>
<tr>
  <th>Timestamp</th>
  <th>Type</th>
  <th>Who</th>
  <th>Detail</th>
</tr>
</thead>
<tbody>
{body}
</tbody>
</table>"""

    @staticmethod
    def _escape(text: str) -> str:
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))
