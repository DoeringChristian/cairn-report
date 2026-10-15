"""`cairn verify` for the object API: re-hash what this machine holds (store/verify.py has the checks).

    verify(store, ref)                   one version: its manifest digest, its materialized copy (cache or pending),
                                         the blobs this machine holds for it, and in a local store its blobs
    verify(store, cache=True)            the pull cache (each cached version against its manifest and digest, each
                                         blob against its name) and this machine's pending versions
    verify(store, store_root=True)       a local store: blobs, versions' blobs, digests
    repair=True                          cache only: drop broken cached versions and blobs; the next pull downloads them
"""

from __future__ import annotations

import os
from pathlib import Path

from ..store import LocalStore, refs
from ..store.cache import blob_cache, cache_root, files_folder
from ..store.digest import manifest_digest, sha256_file
from ..store.verify import Report, check_entries, verify_cache, verify_store
from . import offline, replica


def verify_replica(repair: bool = False) -> Report:
    """The cache (repairable) and every data root's pending versions (never repaired: they may be the only copy)."""
    report = Report()
    needed: set[str] = set()
    for root in replica.data_roots():
        with replica.data_root_at(root):
            needed |= replica.needed()
    report.merge(verify_cache(cache_root(), repair, synced=True, keep=needed))
    for root in replica.data_roots():  # pending versions: reported, never repaired
        report.merge(verify_cache(root, False, synced=False))
    return report


def verify_ref(store, ref: str, repair: bool = False) -> Report:
    """One version: digest of its manifest, its materialized folder (cache or pending), the blobs held for it."""
    report = Report()
    parsed = refs.parse(ref)
    project, name, version = offline.resolve_version(store, parsed)
    label = replica.exact_ref(project, name, version)
    report.count("versions")
    entries, digest = version["files"], version.get("digest")
    if digest is not None and manifest_digest(entries) != digest:
        report.add("digest", label, f"the manifest's files hash to {manifest_digest(entries)}, not the recorded {digest}")
    elif digest is None and version.get("status") == "completed":
        report.add("digest", label, "the version has no manifest digest")
    roots = [cache_root(), *replica.data_roots()]
    for root in roots:
        folder = files_folder(root, version["uid"])
        if folder.is_dir():
            check_entries(folder, entries, report, None, label)
        for entry in entries:
            blob = blob_cache(root, entry["sha256"]) if "sha256" in entry else None
            if blob is not None and blob.is_file():
                report.count("blobs")
                sha, _ = sha256_file(blob)
                if sha != entry["sha256"]:
                    problem = report.add("blob", blob, f"its contents hash to {sha[:16]}..., not {entry['sha256'][:16]}...")
                    if repair and root == cache_root():
                        blob.unlink(missing_ok=True)
                        problem.repaired = True
    if isinstance(store, LocalStore):
        for entry in entries:
            if "sha256" not in entry:
                continue
            report.count("blobs")
            try:
                sha, size = sha256_file(store.blob_path(entry["sha256"]))
            except FileNotFoundError:
                report.add("blob", label, f"{entry['path']}: the blob {entry['sha256'][:16]}... is missing from the store")
                continue
            if sha != entry["sha256"]:
                report.add("blob", store.blob_path(entry["sha256"]), f"{entry['path']}: its contents hash to {sha[:16]}..., not {entry['sha256'][:16]}...")
    return report


def verify(store, ref: str | None = None, *, cache: bool = False, local_store: bool = False, everything: bool = False,
           repair: bool = False) -> Report:
    """See the module doc; `store` is a backend (LocalStore or RemoteStore). Nothing selected means the cache."""
    report = Report()
    if ref:
        report.merge(verify_ref(store, ref, repair))
    if cache or everything or not (ref or local_store):
        report.merge(verify_replica(repair))
    if local_store or everything:
        if isinstance(store, LocalStore):
            report.merge(store.verify())
        elif local_store:
            raise ValueError("the configured store is a server: only a local store folder can be verified (the server's own folder: `cairn verify --store <root>`)")
    return report
