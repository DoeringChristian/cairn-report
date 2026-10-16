"""Browser sessions: what a logged-in browser acts as, and the cookie that names it.

The UI knows one token at a time (the owner's). A logged-in browser holds only the session id, an HMAC of that token
with a fixed label: it carries no secret, survives restarts of the UI, and stops working exactly when the token changes.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

COOKIE = "cairn_ui"
COOKIE_SECONDS = 10 * 365 * 86400  # the cookie's Max-Age: a session does not expire
LOGIN_DELAY = 1.0  # seconds a wrong token waits, which slows down guessing
LABEL = b"cairn-ui-session-v1"


def session_id(token: str) -> str:
    """The session id of a token: an HMAC with a fixed label (the cookie never carries the token itself)."""
    return hmac.new(token.encode(), LABEL, hashlib.sha256).hexdigest()


@dataclass
class Session:
    """What a browser session acts as: a store handle carrying the token."""

    store: object  # the repo interface (store/interface.py): the project's repo, a repo folder, or a remote
