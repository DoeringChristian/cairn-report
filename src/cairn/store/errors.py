"""The store's errors; the server maps them to HTTP statuses and the client raises them back.

Every error Cairn raises on purpose (an unknown artifact, a refused alias, a remote that does not answer, an invalid
reference, ...) is a StoreError. The public functions and methods raise them from the caller's call (cairn/api.py),
so a traceback ends at the user's line and that one call."""

from __future__ import annotations


class StoreError(ValueError):
    pass


class InvalidRef(StoreError):
    """A reference, artifact name, alias, or label that is not well formed."""


class IncompleteRun(StoreError):
    """The folder's run.json says the run is not completed."""


class NoRun(StoreError, RuntimeError):
    """`cairn.run` used in a process that has no run."""


class NotFound(StoreError, LookupError):
    pass


class Ambiguous(StoreError, LookupError):
    """A short id that names several versions; `candidates` lists them ({name, version, uid, short_id, status})."""

    def __init__(self, message: str, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


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
    """The remote did not answer (refused, timed out, or a proxy said 502/503/504)."""


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
        super().__init__(message or f"{len(self.missing)} blobs are not in the repo (yet), e.g. "
                         f"{self.missing[0] if self.missing else '?'}: upload them first")
