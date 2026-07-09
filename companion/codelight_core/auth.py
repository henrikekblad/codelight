from __future__ import annotations

import hashlib
import hmac


def auth_hmac(secret: str, nonce: str) -> str:
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def valid_auth_response(data: dict, secret: str, nonce: str) -> bool:
    """Validate challenge-response auth without accepting plaintext secrets."""
    expected = auth_hmac(secret, nonce)
    provided = str(data.get("auth_hmac") or "")
    return bool(provided) and hmac.compare_digest(provided, expected)
