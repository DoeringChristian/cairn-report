"""The repo's client side and the object API: `Client` (http.py) talks to a server, `Remote` (remote.py) is a project on
it (`<server>/<project>`); `SyncedRepo` (repo.py) is a repo with a remote, its outbox (store/outbox.py) and sending
it (sync.py); `open_repo` (open.py) opens the project's repo (a StandaloneRepo, or a SyncedRepo when `[repo] remote`
is set); the object API (objects.py: `connect`, `Repo`, `Artifact`, `Version`), publishing and `Publication`
(publishing.py). drafts.py: folders managed by Cairn that become a version when they close (`cairn.draft`), runs.py:
the process's run (`cairn.init`), system.py and code.py: what a run records about the machine and the source,
logger.py: the writer runs and drafts share."""

from __future__ import annotations

from .drafts import Draft, current_draft, draft
from .http import Client, ClientError
from .objects import Artifact, File, Repo, Version, connect
from .open import open_repo
from .publishing import Publication, publish
from .remote import Remote, Server
from .repo import SyncedRepo
from .runs import Run, init

__all__ = ["Artifact", "Client", "ClientError", "Draft", "File", "current_draft", "draft", "Publication", "Remote", "Run", "Repo",
           "Server", "SyncedRepo", "Version", "connect", "init", "open_repo", "publish"]
