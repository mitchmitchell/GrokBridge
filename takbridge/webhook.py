"""Signed webhook calls to wake a bot.

The body is JSON. Header X-TakBridge-Signature is the hex HMAC-SHA256 of the
exact body bytes with the shared secret. A failed call never loses the
message: it stays unread in the inbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("takbridge.webhook")

SIGNATURE_HEADER = "X-TakBridge-Signature"


def encode(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(body: bytes, secret: str, signature: str) -> bool:
    """What a bot runs to check a call came from the bridge."""
    return hmac.compare_digest(sign(body, secret), signature or "")


def _post(url: str, body: bytes, signature: str, timeout: float,
          extra: dict | None = None) -> int:
    headers = dict(extra or {})  # e.g. Authorization for a Cursor automation
    headers.update({
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
        "User-Agent": "tak-bridge/0.1",
    })
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (http(s) only, checked in config)
        return resp.status


async def notify(url: str, body: dict, secret: str, *, timeout: float = 10.0,
                 retries: int = 3, backoff: float = 1.0,
                 headers: dict | None = None) -> bool:
    """POST once plus `retries` more times with doubling waits. True on 2xx.
    `headers` are sent as given and never logged."""
    data = encode(body)
    sig = sign(data, secret)
    delay = backoff
    for attempt in range(retries + 1):
        try:
            status = await asyncio.to_thread(_post, url, data, sig, timeout, headers)
            if 200 <= status < 300:
                log.info("webhook %s: delivered message %s", body.get("bot"),
                         body.get("message_id"))
                return True
            log.warning("webhook %s: HTTP %s", body.get("bot"), status)
        except urllib.error.HTTPError as exc:
            log.warning("webhook %s: HTTP %s", body.get("bot"), exc.code)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("webhook %s: %s", body.get("bot"), getattr(exc, "reason", exc))
        if attempt < retries:
            await asyncio.sleep(delay)
            delay *= 2
    log.error("webhook %s: gave up on message %s; it stays unread in the inbox",
              body.get("bot"), body.get("message_id"))
    return False
