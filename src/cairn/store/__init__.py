"""The core: projects, artifacts, and immutable versions in a repo folder (`LocalRepo`), their references, blobs,
tokens, and the read-only folders pulls make. No HTTP, no reports, no UI: everything else builds on this."""

from __future__ import annotations

from .folders import materialize, pull
from .errors import BadRange, IncompleteRun, Conflict, Deleted, Forbidden, MissingBlobs, NotFound, Protected, StoreError, Unauthorized, Unreachable
from .inputs import INPUTS_ENV, INPUTS_FILE, drop_inputs, read_inputs, record_input, run_status
from .local import FileRead, LocalRepo
from .refs import Ref
from .uids import new_uid

__all__ = ["BadRange", "IncompleteRun", "Conflict", "Deleted", "FileRead", "Forbidden", "MissingBlobs", "Protected", "INPUTS_ENV", "INPUTS_FILE", "LocalRepo", "NotFound", "Ref",
           "StoreError", "Unauthorized", "Unreachable", "materialize", "new_uid", "pull", "read_inputs", "drop_inputs", "run_status",
           "record_input", "repo_root"]
