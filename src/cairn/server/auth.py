"""Who a request acts as: the owner token, scoped tokens, and the project checks every route makes."""

from __future__ import annotations

import hmac

from ..store.tokens import OWNER, Principal, Tokens


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Auth:
    """Bearer-token authentication: the owner token, the scoped tokens of `tokens`, or (`no_auth`) everyone as the owner."""

    def __init__(self, owner_token: str, tokens: Tokens, no_auth: bool = False):
        self.owner_token, self.tokens, self.no_auth = owner_token, tokens, no_auth

    def principal_for(self, token: str) -> Principal | None:
        if hmac.compare_digest(token.encode(), self.owner_token.encode()):
            return OWNER
        return self.tokens.find(token)

    def bearer(self, header: str | None) -> Principal | None:
        """The principal of an `Authorization` header value, or None (no or an unknown token)."""
        if self.no_auth:
            return OWNER
        header = header or ""
        return self.principal_for(header[7:].strip()) if header.startswith("Bearer ") else None

    def require(self, header: str | None) -> Principal:
        principal = self.bearer(header)
        if principal is None:
            raise HttpError(401, "missing or invalid token")
        return principal


def allow(principal: Principal, project: str, write: bool = False) -> None:
    if not principal.can(project, write):
        raise HttpError(403, f"this token cannot {'write' if write else 'read'} project {project!r}")
