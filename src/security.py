"""
security.py

Webhook signature verification for all inbound requests.
Keeps all auth logic in one place.
"""

import hmac
import hashlib
import os
import time
from flask import Request


# ── Slack ─────────────────────────────────────────────────────────────────────

def verify_slack_signature(req: Request) -> bool:
    """
    Verify Slack's HMAC-SHA256 request signature.
    Rejects requests older than 5 minutes to prevent replay attacks.
    """
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")

    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    received = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(expected, received)


# ── Recall.ai ─────────────────────────────────────────────────────────────────

def verify_recall_signature(req: Request) -> bool:
    """
    Verify Recall.ai's webhook signature.
    Recall signs payloads using HMAC-SHA256 with your API key as the secret,
    and sends the signature in the X-Recall-Signature header.

    If RECALL_WEBHOOK_SECRET is not set, falls back to checking the request
    comes with a valid Authorization header matching the API key.
    Logs a warning so you know verification is weakened.
    """
    webhook_secret = os.environ.get("RECALL_WEBHOOK_SECRET", "")

    if webhook_secret:
        received_sig = req.headers.get("X-Recall-Signature", "")
        if not received_sig:
            print("SECURITY: Recall webhook missing X-Recall-Signature header")
            return False
        expected = hmac.new(
            webhook_secret.encode(),
            req.get_data(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, received_sig)

    # Fallback: verify the Authorization header matches our API key
    # Less secure but better than nothing while you set up the webhook secret
    api_key = os.environ.get("RECALL_API_KEY", "")
    auth_header = req.headers.get("Authorization", "")
    if api_key and auth_header:
        return hmac.compare_digest(f"Token {api_key}", auth_header)

    print("SECURITY WARNING: RECALL_WEBHOOK_SECRET not set — Recall webhook is unverified")
    return True  # Permissive fallback — set RECALL_WEBHOOK_SECRET to harden


# ── Confluence ────────────────────────────────────────────────────────────────

def verify_confluence_signature(req: Request) -> bool:
    """
    Verify Confluence webhook authenticity.

    Confluence webhook requests include a shared secret you define when
    creating the webhook. It's sent as a JWT in the Authorization header
    or as a plain token in X-Atlassian-Token depending on your Confluence version.

    We use a shared secret approach: set CONFLUENCE_WEBHOOK_SECRET to any
    strong random string, configure the same value in Confluence when creating
    the webhook, and we verify it here.
    """
    webhook_secret = os.environ.get("CONFLUENCE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        print("SECURITY WARNING: CONFLUENCE_WEBHOOK_SECRET not set — Confluence webhook is unverified")
        return True  # Permissive fallback — set the secret to harden

    # Confluence sends the secret as a query parameter or in the header
    # depending on version — check both
    provided = (
        req.args.get("secret")
        or req.headers.get("X-Atlassian-Webhook-Secret")
        or req.headers.get("Authorization", "").replace("Bearer ", "")
    )

    if not provided:
        print("SECURITY: Confluence webhook missing secret")
        return False

    return hmac.compare_digest(webhook_secret, provided)


# ── Rate limiting (simple in-memory) ─────────────────────────────────────────

_request_counts: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX = 30      # max requests per IP per window


def check_rate_limit(ip: str) -> bool:
    """
    Simple sliding window rate limiter.
    Returns True if request is allowed, False if rate limit exceeded.
    """
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    if ip not in _request_counts:
        _request_counts[ip] = []

    # Drop timestamps outside the window
    _request_counts[ip] = [t for t in _request_counts[ip] if t > window_start]
    _request_counts[ip].append(now)

    if len(_request_counts[ip]) > RATE_LIMIT_MAX:
        print(f"SECURITY: Rate limit exceeded for IP {ip}")
        return False
    return True
