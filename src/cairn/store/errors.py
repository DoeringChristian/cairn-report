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


class Conflict(StoreError):
    """The request contradicts what is stored, e.g. a known version uid with different content (HTTP 409)."""


class Unreachable(StoreError, OSError):
    """The store server did not answer (refused, timed out, or a proxy said 502/503/504)."""


class Deleted(NotFound):
    """The version was deleted: its tombstone keeps the number and uid, never reused (HTTP 410)."""


class Protected(Conflict):
    """A deletion refused because versions are protected (HTTP 409); `protected` lists {ref, reasons}."""

    def __init__(self, message: str, protected=()):
        super().__init__(message)
        self.protected = list(protected)


class MissingBlobs(Conflict):
    """A version names blobs the store does not have (never uploaded, or removed by garbage collection since the
    client checked): upload them and send the version again (HTTP 409 with `missing`)."""

    def __init__(self, missing=(), message: str | None = None):
        self.missing = sorted(missing)
        super().__init__(message or f"{len(self.missing)} blobs are not in the store (yet), e.g. "
                         f"{self.missing[0] if self.missing else '?'}: upload them first")
