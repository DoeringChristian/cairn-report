"""The store server: a LocalStore root shared over HTTP, behind tokens. Only an artifact store: a JSON API and
file downloads, no pages (`cairn ui` explores a store in a browser, locally or against this server).

    cairn server <root> [--host 127.0.0.1] [--port 8800] [--max-blob-gb 50] [--no-auth]

A new root gets a fresh owner token on first launch; every launch prints `Store: <url>` and
`Token: <owner token>` to the terminal (never into a URL). Scoped tokens (one project, read or
write) are made with `cairn token create` (see store/tokens.py). Bind localhost, and put an
HTTPS reverse proxy in front of anything else: the server speaks plain HTTP.

Artifact names are slash paths (`datasets/crops`), so every route says where the name ends:

    API (JSON, `Authorization: Bearer <token>`; a name is ONE percent-encoded path segment, `datasets%2Fcrops`)
      GET  /api/v2/projects                                      [{..., artifacts, updated}]   POST {name, title?, description?}
      GET  /api/v2/projects/<p>                                  PATCH {title?, description?}
      GET  /api/v2/projects/<p>/artifacts                        [{name, latest, versions, aliases, updated, metadata}]
      GET  /api/v2/projects/<p>/artifacts/<name>                 that summary plus `latest_version` (its manifest, or null)
      GET  /api/v2/projects/<p>/artifacts/<name>/versions        every version, full manifests
      GET  /api/v2/projects/<p>/artifacts/<name>/versions/<n>    one manifest        .../versions/<n>/files  {files: [entries]}
      GET  /api/v2/projects/<p>/artifacts/<name>/aliases         {alias: version}
      POST /api/v2/projects/<p>/artifacts/<name>/versions        {label?, metadata?, source?} -> {version} (running)
      POST .../versions/<n>/files {entries}   (409 + `missing` when blobs are absent)   .../complete {inputs?}   .../fail
      POST /api/v2/projects/<p>/artifacts/<name>/publish         {entries, label?, metadata?, inputs?, aliases?, source?}
                                                                 -> {version, created}  (created=false: nothing changed)
      POST /api/v2/projects/<p>/artifacts/<name>/aliases         {alias, version}
      GET  /api/v2/resolve?ref=<ref>                             {project, name, version: manifest}
      GET  /api/v2/lineage?ref=<ref>                             {ref, inputs, consumers}
      POST /api/v2/blobs/missing {sha256: [...]}                 PUT /api/v2/blobs/<sha256> (streamed, hash-verified)
    Files (the name ends at `@`; the path inside the artifact follows `//`)
      GET  /files/<p>/<name>[@v<n>|@alias|@label]//<path>        ETag, Range, immutable for `@v<n>` of a completed
                                                                 version, `?download=1` for an attachment

References in the API are `project:name[@selector]` (the project is required: the server has no default).

Tokens are bearer tokens only: there are no cookies and no pages. Everything stored is append-only and the
server never executes pushed code. Files are served with `Content-Security-Policy: sandbox`.
"""

from __future__ import annotations

from .app import make_server, serve_store

__all__ = ["make_server", "serve_store"]
