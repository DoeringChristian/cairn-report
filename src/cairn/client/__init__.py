"""The repo's client side and the object API: `Client` (http.py) and `Remote` (remote.py) talk to a remote;
`SyncedRepo` (repo.py) is a repo with a remote, its outbox (outbox.py) and sending it (sync.py); `open_repo`
(open.py) opens the project's repo (a LocalRepo, or a SyncedRepo when `[repo] remote` is set); the object API
(objects.py: `connect`, `Repo`, `Project`, `Artifact`, `Version`), publishing and `Publication` (publishing.py),
`cairn prune` (prune.py) and `cairn verify` (verify.py). drafts.py: folders managed by Cairn that become a version
when they close (`cairn.draft`), runs.py: the process's run (`cairn.init`), system.py and code.py: what a run
records about the machine and the source, logger.py: the writer runs and drafts share."""

from __future__ import annotations

from .drafts import Draft, current_draft, draft
from .http import Client, ClientError
from .objects import Artifact, File, Project, Repo, Version, connect
from .open import open_repo
from .publishing import Publication, publish
from .remote import Remote
from .repo import SyncedRepo
from .runs import Run, init

__all__ = ["Artifact", "Client", "ClientError", "Draft", "File", "current_draft", "draft", "Project", "Publication", "Remote", "Run", "Repo",
           "SyncedRepo", "Version", "connect", "init", "open_repo", "publish"]
