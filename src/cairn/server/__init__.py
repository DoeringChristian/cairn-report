"""`cairn server`: a repo (a LocalRepo folder) shared over HTTP, behind one token: the remote of other machines' repos.

A server hosts many projects in its one repo folder, each at `<server>/<project>` as if it were its own repo, sharing
one database and one content-addressed `blobs/`; clients name one with `cairn remote set <server>/<project>`. Only a
JSON API and file downloads, no pages. The routes, the token rules, and how the server is built are in app.py.
"""

from __future__ import annotations

from .app import make_server, serve_store

__all__ = ["make_server", "serve_store"]
