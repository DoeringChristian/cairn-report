"""Browser sessions: what a logged-in browser acts as, and the cookie that names it."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store import LocalRepo
from ..store.tokens import Principal
from ..client.remote import Remote

COOKIE = "cairn_ui"
SESSION_SECONDS = 30 * 86400
MAX_SESSIONS = 1000
LOGIN_DELAY = 1.0  # seconds a wrong token waits, which slows down guessing


@dataclass
class Session:
    """What a browser session acts as: a store handle (carrying the user's token) and, for a folder, its principal."""
    store: LocalRepo | Remote
    principal: Principal | None  # None: no restriction here (a remote store enforces its own)
    expires: float = 0.0
    cache: dict = field(default_factory=dict)
