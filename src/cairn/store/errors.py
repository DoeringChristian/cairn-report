"""The store's errors; the server maps them to HTTP statuses and the client raises them back."""

from __future__ import annotations


class StoreError(ValueError):
    pass


class NotFound(StoreError, LookupError):
    pass


class Forbidden(StoreError):
    """The token is valid but may not read or write this (HTTP 403)."""


class Unauthorized(StoreError):
    """The store does not accept the token (HTTP 401)."""


class BadRange(StoreError):
    """A byte range outside the file (HTTP 416); `total` is the file's size."""

    def __init__(self, total: int):
        super().__init__(f"the range lies outside the file ({total} bytes)")
        self.total = total
