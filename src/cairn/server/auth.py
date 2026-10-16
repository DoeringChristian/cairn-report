"""Who may make a request: the holder of the repo's one token (the owner's), or everyone with `no_auth`."""

from __future__ import annotations

import hmac


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Auth:
    """Bearer-token authentication against the owner token, or (`no_auth`) none."""

    def __init__(self, owner_token: str, no_auth: bool = False):
        self.owner_token, self.no_auth = owner_token, no_auth

    def valid(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.owner_token.encode())

    def bearer(self, header: str | None) -> bool:
        """Whether an `Authorization` header value carries the owner token."""
        if self.no_auth:
            return True
        header = header or ""
        return header.startswith("Bearer ") and self.valid(header[7:].strip())

    def require(self, header: str | None) -> None:
        if not self.bearer(header):
            raise HttpError(401, "missing or invalid token")
