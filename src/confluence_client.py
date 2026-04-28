import os
import re
import requests
from requests.auth import HTTPBasicAuth


class ConfluenceClient:
    def __init__(self):
        self.base_url = os.environ["CONFLUENCE_BASE_URL"].rstrip("/")
        self.auth = HTTPBasicAuth(
            os.environ["CONFLUENCE_EMAIL"],
            os.environ["CONFLUENCE_API_TOKEN"]
        )
        self.headers = {"Accept": "application/json"}

    def get_gold_standard_spec(self) -> str:
        """
        Fetch the gold standard spec page used as a formatting example.
        Page ID is set via CONFLUENCE_GOLD_STANDARD_PAGE_ID env var.
        Returns empty string if not configured, so the system degrades gracefully.
        """
        page_id = os.environ.get("CONFLUENCE_GOLD_STANDARD_PAGE_ID", "")
        if not page_id:
            return ""
        try:
            return self.get_page_content(page_id)
        except Exception as e:
            print(f"Could not fetch gold standard spec (page {page_id}): {e}")
            return ""

    def search(self, query: str, limit: int = 3) -> list[dict]:
        """Search Confluence for pages matching the query."""
        space_key = os.environ.get("CONFLUENCE_SPACE_KEY", "")
        cql = f'text ~ "{query}" AND type = "page"'
        if space_key:
            cql += f' AND space = "{space_key}"'

        resp = requests.get(
            f"{self.base_url}/rest/api/content/search",
            params={"cql": cql, "limit": limit, "expand": "space"},
            auth=self.auth,
            headers=self.headers,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [{"id": r["id"], "title": r["title"]} for r in results]

    def get_page_content(self, page_id: str) -> str:
        """Fetch and clean the text content of a Confluence page."""
        resp = requests.get(
            f"{self.base_url}/rest/api/content/{page_id}",
            params={"expand": "body.storage"},
            auth=self.auth,
            headers=self.headers,
        )
        resp.raise_for_status()
        html = resp.json()["body"]["storage"]["value"]
        # Strip HTML tags for plain text
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]  # Cap at ~8k chars per page to stay within context limits

    def page_url(self, page_id: str) -> str:
        return f"{self.base_url}/pages/{page_id}"

    def get_page_raw_html(self, page_id: str) -> str:
        """Fetch the raw Confluence storage HTML for a page (for log appending)."""
        resp = requests.get(
            f"{self.base_url}/rest/api/content/{page_id}",
            params={"expand": "body.storage"},
            auth=self.auth,
            headers=self.headers,
        )
        resp.raise_for_status()
        return resp.json()["body"]["storage"]["value"]

    def create_draft_page(self, title: str, content: str, space_key: str) -> str:
        """Create a new Confluence page in draft state and return its URL."""
        html_content = self._markdown_to_confluence(content)

        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": html_content,
                    "representation": "storage"
                }
            },
            "status": "draft"
        }

        resp = requests.post(
            f"{self.base_url}/rest/api/content",
            json=payload,
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        page_id = resp.json()["id"]
        return self.page_url(page_id)

    def publish_page(self, page_id: str) -> None:
        """Change a draft page to published status."""
        resp = requests.get(
            f"{self.base_url}/rest/api/content/{page_id}",
            params={"expand": "version"},
            auth=self.auth,
            headers=self.headers,
        )
        resp.raise_for_status()
        current_version = resp.json()["version"]["number"]
        payload = {
            "type": "page",
            "status": "current",
            "version": {"number": current_version + 1},
        }
        requests.put(
            f"{self.base_url}/rest/api/content/{page_id}",
            json=payload,
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
        )

    def update_page(self, page_id: str, title: str, content: str, raw_html: bool = False) -> str:
        """Update an existing Confluence page with new content."""
        resp = requests.get(
            f"{self.base_url}/rest/api/content/{page_id}",
            params={"expand": "version"},
            auth=self.auth,
            headers=self.headers,
        )
        resp.raise_for_status()
        current_version = resp.json()["version"]["number"]

        html_content = content if raw_html else self._markdown_to_confluence(content)
        payload = {
            "type": "page",
            "title": title,
            "version": {"number": current_version + 1},
            "body": {
                "storage": {
                    "value": html_content,
                    "representation": "storage"
                }
            },
        }
        resp = requests.put(
            f"{self.base_url}/rest/api/content/{page_id}",
            json=payload,
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return self.page_url(page_id)

    def search_by_title(self, title: str, limit: int = 5) -> list[dict]:
        """Search Confluence pages by title — exact match first, then contains."""
        space_key = os.environ.get("CONFLUENCE_SPACE_KEY", "")
        for operator in ("=", "~"):
            cql = f'title {operator} "{title}" AND type = "page"'
            if space_key:
                cql += f' AND space = "{space_key}"'
            resp = requests.get(
                f"{self.base_url}/rest/api/content/search",
                params={"cql": cql, "limit": limit, "expand": "space"},
                auth=self.auth,
                headers=self.headers,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return [{"id": r["id"], "title": r["title"]} for r in results]
        return []

    def extract_section(self, raw_html: str, heading_text: str) -> dict | None:
        """
        Extract a section from Confluence storage HTML by heading text.
        Returns {"heading_tag": str, "full_html": str} or None.
        full_html spans from the heading to the next equal-or-higher heading.
        """
        pattern = re.compile(
            r'(<h([1-6])[^>]*>[^<]*' + re.escape(heading_text) + r'[^<]*</h\2>)',
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(raw_html)
        if not match:
            return None

        heading_level = int(match.group(2))
        end_pattern = re.compile(r'<h([1-' + str(heading_level) + r'])[^>]*>', re.IGNORECASE)
        end_match = end_pattern.search(raw_html, match.end())
        end_pos = end_match.start() if end_match else len(raw_html)

        return {
            "heading_tag": match.group(1),
            "full_html": raw_html[match.start():end_pos],
        }

    def replace_section_html(self, page_id: str, page_title: str, old_html: str, new_html: str) -> str:
        """Replace a section's HTML in a Confluence page and return the page URL."""
        current_html = self.get_page_raw_html(page_id)
        if old_html not in current_html:
            raise ValueError("Section HTML not found — the page may have changed since you fetched it.")
        updated_html = current_html.replace(old_html, new_html, 1)
        return self.update_page(page_id=page_id, title=page_title, content=updated_html, raw_html=True)

    def _markdown_to_confluence(self, text: str) -> str:
        """Basic markdown → Confluence storage format conversion."""
        lines = []
        for line in text.split("\n"):
            if line.startswith("## "):
                lines.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("# "):
                lines.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("### "):
                lines.append(f"<h3>{line[4:]}</h3>")
            elif line.startswith("- ") or line.startswith("* "):
                lines.append(f"<li>{line[2:]}</li>")
            elif line.strip() == "":
                lines.append("<br/>")
            else:
                lines.append(f"<p>{line}</p>")
        return "\n".join(lines)
