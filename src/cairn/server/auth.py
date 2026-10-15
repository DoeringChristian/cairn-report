"""Who a request acts as: the owner token, scoped tokens, and the project checks every route makes."""

from __future__ import annotations

import hmac

from ..store.tokens import OWNER, Principal, Tokens


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class AuthMixin:
    """Bearer-token authentication for the handler (needs `owner_token`, `tokens`, `no_auth`, `headers`)."""

    owner_token: str = ""
    tokens: Tokens
    no_auth: bool = False  # --no-auth: every request acts as the owner (local testing only)

    def _principal_for(self, token: str) -> Principal | None:
        if hmac.compare_digest(token.encode(), self.owner_token.encode()):
            return OWNER
        return self.tokens.find(token)

    def _bearer(self) -> Principal | None:
        if self.no_auth:
            return OWNER
        header = self.headers.get("Authorization", "")
        return self._principal_for(header[7:].strip()) if header.startswith("Bearer ") else None

    @staticmethod
    def _allow(principal: Principal, project: str, write: bool = False) -> None:
        if not principal.can(project, write):
            raise HttpError(403, f"this token cannot {'write' if write else 'read'} project {project!r}")
