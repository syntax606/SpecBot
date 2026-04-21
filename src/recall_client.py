import os
import requests


class RecallClient:
    """
    Wraps the Recall.ai REST API.
    Docs: https://docs.recall.ai
    """

    def __init__(self):
        self.api_key = os.environ["RECALL_API_KEY"]
        self.region = os.environ.get("RECALL_REGION", "us-east-1")
        self.base_url = f"https://{self.region}.recall.ai/api/v1"
        self.headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.webhook_url = os.environ["RECALL_WEBHOOK_URL"]  # e.g. https://yourapp.railway.app/recall/webhook

    # ── Bot lifecycle ─────────────────────────────────────────────────────────

    def create_bot(self, meeting_url: str, session_id: str) -> dict:
        """
        Send a bot to a Google Meet / Zoom / etc. call.
        Returns the bot object including its id.
        session_id is our internal identifier (e.g. Slack thread ts) stored
        in bot metadata so we can route transcript chunks back correctly.
        """
        payload = {
            "meeting_url": meeting_url,
            "bot_name": "SpecBot 📋",
            "transcription_options": {
                "provider": "recall_ai"  # built-in transcription ($0.15/hr)
            },
            "real_time_transcription": {
                "destination_url": self.webhook_url,
                "partial_results": False,   # only send completed utterances
            },
            "metadata": {
                "session_id": session_id,   # echoed back in every webhook payload
            },
        }
        resp = requests.post(f"{self.base_url}/bot", json=payload, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def remove_bot(self, bot_id: str) -> None:
        """Kick the bot out of the call."""
        resp = requests.post(
            f"{self.base_url}/bot/{bot_id}/leave_call",
            headers=self.headers
        )
        resp.raise_for_status()

    def get_bot(self, bot_id: str) -> dict:
        resp = requests.get(f"{self.base_url}/bot/{bot_id}", headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    # ── Webhook parsing ───────────────────────────────────────────────────────

    @staticmethod
    def parse_transcript_chunk(payload: dict) -> dict | None:
        """
        Extract a normalised transcript chunk from a Recall webhook payload.
        Returns:
            {
                "session_id": str,
                "bot_id": str,
                "speaker": str,
                "text": str,
                "is_final": bool,
            }
        or None if the payload isn't a transcript event.
        """
        event_type = payload.get("event")
        if event_type != "transcript.data":
            return None

        data = payload.get("data", {})
        words = data.get("words", [])
        if not words:
            return None

        text = " ".join(w.get("text", "") for w in words).strip()
        if not text:
            return None

        return {
            "session_id": payload.get("metadata", {}).get("session_id", ""),
            "bot_id": payload.get("bot_id", ""),
            "speaker": data.get("participant", {}).get("name", "Unknown"),
            "text": text,
            "is_final": True,
        }
