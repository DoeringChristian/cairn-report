"""The sync engine: moves a repo's outbox entries to its remote and records the remote's answers (numbers) in the
repo. Pure mechanism: it uses the outbox (store/outbox.py), the remote (the repo interface, store/interface.py, over
HTTP), and the repo's copies (a LocalRepo, or a stand-in without a database), and nothing else: no configuration, no
printing (what happened comes back as a report), no threads.

A publish in a repo with a remote first commits the version to the outbox: an entry with the artifact name, the
manifest entries, label, metadata, inputs, aliases, and source, and the contents it needs kept in the repo's blobs
(client/repo.py stages them), so the upload never reads the source folder. The remote is one project of a server
(`<server>/<project>`), and the repo's copies are its one project: neither call names a project.

Kinds and sync states:
- `publish` (`sync: pending`): one finished version, sent with the publish route.
- `running` (`sync: running`, `status: running`): a running version (a run, `cairn publish --watch`). It is created
  on the remote under its uid as soon as the remote answers, its files are added as they are synced, and its owner
  rewrites `heartbeat` (time, host, pid) while it runs. Completion or failure turns it `pending` (status completed
  or failed) and it is sent like any publish. Any sender on any host fails it (a queued failure) once its heartbeat
  is older than `[repo] heartbeat_timeout` (600 s); on the owner's host a dead pid fails it at once.
- `rejected`: the remote refused it (an error other than not answering); kept for `cairn status` and skipped by
  every sync. Delete its folder to drop it.

What a send does, per entry: the blobs the remote lacks first, then the manifest; then the remote's answer (the
version with its number) is written into the repo (a copy, with the remote's aliases), and only after that is the
entry removed. Sending is idempotent because every request carries the version's uid: the remote answers a version
it already has with that version, so an entry sent twice (a sync killed after the remote's answer, two hosts
sending one shared outbox) makes one version. To avoid double work, a sender claims an entry first
(`Outbox.claim`). Entries are sent in uid order, which is creation order, so a pending version that consumed
another pending version (its input is `name@<uid>`) follows it, and the remote turns that input into `name@v<n>`.

A repo that numbered versions itself before it had a remote hands them to the outbox at its first sync (`adopt`),
oldest first, with their inputs named by uid; the remote numbers them, and they come back as copies.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..store import refs
from ..store.blobs import blob_path, now
from ..store.digest import manifest_digest
from ..store.errors import Forbidden, MissingBlobs, NotFound, StoreError, Unauthorized, Unreachable
from ..store.folders import hostname, update_record
from ..store.local import read_repo_file, repo_uid, update_repo_file
from ..store.outbox import Outbox, belongs
from ..store.refs import Ref

RETRIES = 3  # sends of a version whose blobs the remote's garbage collection removed meanwhile


class NotRecorded(Unreachable):
    """The remote has a version, but the repo could not record its answer yet: the outbox entry stays and the next sync
    records it (sending again is harmless). Never a reason to reject an entry."""


class Waiting(Unreachable):
    """An entry cannot be sent now for a reason that is not the entry's own (the token is refused, an input of it is still
    on its way): it stays pending, and the reason is reported. Never a reason to reject an entry."""


def _input_uids(record: dict) -> list[str]:
    found = []
    for item in record.get("inputs") or []:
        try:
            uid = refs.parse(item).uid
        except ValueError:
            continue
        if uid:
            found.append(uid)
    return found


def _when(record: dict) -> tuple:
    """Publish order: when the version became final (else when it started), then its uid."""
    return (record.get("completed") or record.get("created") or "", record["uid"])


class Sync:
    """One repo's outbox and its remote (see the module doc).

    `remote` is the project on the server (client/remote.py Remote); `copies` the repo's own project in its database (a
    LocalProject: `put_copy`, `mirror_aliases`, `meta`, `set_meta`, `local_versions`, `forget_versions`, `aliases`,
    `get_version`, `copies`) or a stand-in that keeps nothing; `root` the repo folder; `timeout` the heartbeat
    timeout of running versions (seconds)."""

    def __init__(self, outbox: Outbox, remote, copies, root: Path, timeout: float):
        self.outbox, self.remote, self.copies, self.root, self.timeout = outbox, remote, copies, Path(root), timeout

    # ------------------------------------------------------------ the remote

    @property
    def known_uid(self) -> str | None:
        """The remote's uid as the repo last learned it (repo.json), without asking."""
        if self.remote._uid:
            return self.remote._uid
        known = read_repo_file(self.root).get("remote") or {}
        return known.get("uid") if isinstance(known, dict) and known.get("url") == self.remote.url else None

    def _bound(self, uid: str) -> str:
        """What the repo's copies are recorded under (`origin`) and the repo is bound to (meta `remote`): the server's
        uid and the project, `<uid>/<project>`."""
        return f"{uid}/{self.remote.project}"

    def check_remote(self) -> str:
        """The remote's uid, after making sure this repo may sync with it: not the repo itself, and the remote (server
        and project) whose copies the repo holds (a repo has one remote). Remembered in repo.json. Raises Unreachable
        while it does not answer, StoreError when the remote is not this repo's."""
        uid = self.remote.uid
        if uid == repo_uid(self.root):
            raise StoreError(
                f"the remote {self.remote.url} is the server of this very repo ({self.root}); processes on its "
                "machine open the repo directly: `cairn remote remove`"
            )
        bound = self.copies.meta("remote")
        if bound and bound != self._bound(uid):
            raise StoreError(
                f"the repo {self.root} holds the versions of another remote ({bound}: a server's uid and its "
                f"project); the remote {self.remote.url} is a different one ({self._bound(uid)}). A repo has one "
                "remote: set it back with `cairn remote set`, or start a new repo (move the folder away once `cairn status` "
                "shows nothing pending)"
            )
        update_repo_file(self.root, remote={"url": self.remote.url, "uid": uid})
        return uid

    def origin(self) -> str | None:
        """What the repo's copies are recorded under: the remote's server uid and project, if this repo is (or may
        become) its repo."""
        uid = self.known_uid
        if uid is None:
            try:
                uid = self.check_remote()
            except StoreError:
                return None
        bound = self.copies.meta("remote")
        return self._bound(uid) if not bound or bound == self._bound(uid) else None

    # ------------------------------------------------------------ entries

    def records(self) -> list[dict]:
        """This remote's outbox entries, oldest first (by uid)."""
        uid = self.known_uid
        return [record for record in self.outbox.records() if belongs(record, self.remote.url, uid)]

    def pending(self) -> list[dict]:
        """The entries still to send (everything but rejected ones)."""
        return [record for record in self.records() if record["sync"] in ("pending", "running")]

    @staticmethod
    def ordered(records: list[dict]) -> list[dict]:
        """Entries in the order they can be sent: in publish order (when each became final), but every entry after the
        pending entries it names as inputs (a draft's uid can be older than the run it is an output of: the run starts
        when the draft opens). A failed run comes after its outputs: they name it while it runs (`_start_inputs`)."""
        records = sorted(records, key=_when)
        by_uid = {record["uid"]: record for record in records}
        order: list[dict] = []
        state: dict[str, str] = {}

        def visit(record: dict) -> None:
            if state.get(record["uid"]):
                return  # done, or on the way (a cycle cannot be sent in any order: the remote decides)
            state[record["uid"]] = "open"
            for uid in _input_uids(record):
                other = by_uid.get(uid)
                if other is not None and not (other.get("kind") == "running" and other.get("status") == "failed"):
                    visit(other)
            state[record["uid"]] = "done"
            order.append(record)

        for record in records:
            visit(record)
        return order

    def waiting(self) -> bool:
        """Whether anything waits for the remote: an entry to send, or a version the repo numbered itself (no network)."""
        return bool(self.pending()) or bool(self.copies.local_versions())

    # ------------------------------------------------------------ recording the remote's answers

    def record(self, name: str, manifest: dict) -> None:
        """Write a remote's answer into the repo: the version (a copy, with the remote's number) and the artifact's
        aliases as the remote has them (their versions copied first), and the read-only folder's record. Raises when
        the repo cannot record it (then the outbox entry stays, and the next sync records it)."""
        origin = self.origin()
        if origin is not None and manifest.get("status") in ("completed", "failed", "deleted"):
            self.copies.put_copy(name, manifest, origin)
            try:
                self.mirror(name)
            except StoreError:  # best effort: the next refresh mirrors them
                pass
        update_record(self.root, manifest, self.known_uid)

    def mirror(self, name: str, aliases: dict[str, int] | None = None) -> None:
        """Make the repo's aliases of an artifact the remote's (asked now unless given); the versions they point at are
        copied first."""
        origin = self.origin()
        if origin is None:
            return
        if aliases is None:
            aliases = self.remote.aliases(name)
        for number in sorted(set(aliases.values())):
            try:
                self.copies.get_version(name, number)
            except (NotFound, Unreachable):
                manifest = self.remote.get_version(name, number)
                if manifest.get("status") in ("completed", "failed", "deleted"):
                    self.copies.put_copy(name, manifest, origin)
        self.copies.mirror_aliases(name, aliases)

    # ------------------------------------------------------------- sending

    def _upload(self, files: list[dict], what: str | None = None) -> None:
        """Upload the blobs the remote lacks, from the repo."""
        sources = {}
        missing = set(self.remote.missing(files)) if any("sha256" in item for item in files) else set()
        for item in files:
            if item.get("sha256") in missing:
                blob = blob_path(self.root, item["sha256"])
                if not blob.is_file():
                    raise StoreError(
                        f"the staged contents of {item['path']} ({item['sha256'][:12]}) left the repo {self.root}"
                    )
                sources[item["sha256"]] = (blob, item["size"])
        if sources:
            self.remote.upload(sources, None, quiet=True, what=what)

    def push(self, record: dict, claimed=None) -> bool:
        """Send what the remote lacks of an entry; True once the version is final there (and recorded in the repo).

        Raises Unreachable when the remote does not answer, and StoreError when it refuses."""
        remote, name = self.remote, record["name"]
        remote.create()
        if record["status"] == "completed":
            record["digest"] = manifest_digest(
                record["files"]
            )  # the remote recomputes it from the files and refuses a mismatch
        manifest = {key: record.get(key) for key in ("label", "metadata", "source")}
        if record["status"] in ("completed", "failed"):
            self._start_inputs(record)
        if record["kind"] == "publish":
            for attempt in range(RETRIES):
                self._upload(record["files"], name)
                self.outbox.refresh_claim(claimed)
                try:
                    record["number"] = remote.publish(
                        name,
                        record["files"],
                        inputs=record["inputs"],
                        aliases=record["aliases"],
                        uid=record["uid"],
                        digest=record["digest"],
                        **manifest,
                    )
                    break
                except MissingBlobs:  # the remote's garbage collection removed a blob since we checked: send it again
                    if attempt == RETRIES - 1:
                        raise
            return self._done(record)
        if record["number"] is None:
            record["number"] = remote.start_version(name, uid=record["uid"], **manifest)
            record["pushed_metadata"] = record.get("metadata")
            self.outbox.save(record)
        if (
            record.get("pushed_metadata", record.get("metadata")) != record.get("metadata")
            and record["status"] == "running"
        ):
            remote.set_metadata(name, record["number"], record.get("metadata") or {})  # changed while it ran
            record["pushed_metadata"] = record.get("metadata")
            self.outbox.save(record)
        fresh = [
            item
            for item in record["files"]
            if record["pushed"].get(item["path"]) != (item.get("sha256") or item.get("link"))
        ]
        try:
            if fresh:
                for attempt in range(RETRIES):
                    self._upload(fresh, name)
                    self.outbox.refresh_claim(claimed)
                    try:
                        remote.add_files(name, record["number"], fresh)
                        break
                    except MissingBlobs:  # removed by garbage collection since we checked: send it again
                        if attempt == RETRIES - 1:
                            raise
                record["pushed"].update({item["path"]: item.get("sha256") or item.get("link") for item in fresh})
                self.outbox.save(record)
            if record["status"] == "running":
                if not fresh:
                    remote.keep_alive(name, record["number"])  # nothing new: still say it is alive
                return False
            if record["status"] == "completed":
                remote.complete_version(name, record["number"], record["inputs"], digest=record["digest"])
                for alias in record["aliases"]:
                    remote.set_alias(name, alias, record["number"])
            else:
                remote.fail_version(name, record["number"])
        except StoreError as error:
            if isinstance(error, Unreachable) or "can no longer change" not in str(error):
                raise
            final = remote.get_version(name, record["uid"])  # sent before (by us or another process)
            if final["status"] == "running":
                raise
            if final["status"] == "completed" and record["status"] == "completed":
                for alias in record["aliases"]:
                    remote.set_alias(name, alias, record["number"])
        return self._done(record)

    def _start_inputs(self, record: dict) -> None:
        """An input that is a running version of this repo (the run an output comes from; also one that failed since
        and is not sent yet) must exist on the remote before the output names it: create it there now, running
        (idempotent by uid; its owner, or the next sync, sends the rest). A pending input that is final is sent first.
        """
        for uid in _input_uids(record):
            other = self.outbox.load(uid)
            if other is None or other.get("number") is not None:
                continue
            if other.get("kind") != "running":
                if other.get("sync") == "pending":
                    self.send(other)  # a pending version the record consumed: it goes first
                if self.outbox.load(uid) is not None:
                    raise Waiting(
                        f"{record['name']}@{record['uid']} waits for its input {other['name']}@{uid}, which is not sent yet"
                    )
                continue
            manifest = {key: other.get(key) for key in ("label", "metadata", "source")}
            self.remote.create()
            number = self.remote.start_version(other["name"], uid=other["uid"], **manifest)
            current = self.outbox.load(uid)
            if current is not None and current.get("number") is None:
                current["number"] = number
                self.outbox.save(current)

    def _done(self, record: dict) -> bool:
        """The remote has the version: record it in the repo (its number, a copy), then remove the outbox entry. When the
        repo cannot record it yet, the entry stays (sending again is harmless) and this raises."""
        remote, name = self.remote, record["name"]
        try:
            manifest = remote.get_version(name, record["uid"])
        except NotFound:  # nothing changed since the latest: the remote kept its own version, not this uid
            manifest = None
            if record.get("number") is not None:
                try:
                    manifest = remote.get_version(name, record["number"])
                except NotFound:
                    manifest = None
        if manifest is not None and manifest.get("uid") != record["uid"]:
            self._redirect(record, name, manifest)
        if manifest is not None:
            try:
                self.record(name, manifest)
            except Unreachable:
                raise
            except Exception as error:  # noqa: BLE001 - the repo could not record it: keep the entry, never reject it
                raise NotRecorded(
                    f"the remote has {name}@v{manifest.get('version')} (uid {manifest.get('uid')}), but "
                    f"the repo could not record it yet ({type(error).__name__}: {error}); the next sync does"
                ) from error
        self.outbox.remove(record)
        return True

    def _redirect(self, record: dict, name: str, manifest: dict) -> None:
        """The remote kept its own version instead of this entry's (nothing changed since its latest): every other entry
        that names this entry's uid as an input names the remote's version from now on."""
        gone, exact = f"{name}@{record['uid']}", f"{name}@v{manifest['version']}"
        for other in self.outbox.records():
            if gone in (other.get("inputs") or []):
                other["inputs"] = [exact if item == gone else item for item in other["inputs"]]
                self.outbox.save(other)

    def _final_there(self, record: dict) -> bool:
        """Whether the remote has this entry's version final already (sent before, by another sender or a lost answer)."""
        try:
            manifest = self.remote.get_version(record["name"], record["uid"])
        except StoreError:
            return False
        return manifest.get("status") in ("completed", "failed", "deleted") and (
            record["status"] == manifest.get("status") or manifest.get("status") == "deleted"
        )

    def send(self, record: dict, claim: bool = True) -> bool:
        """Push one entry; a refusal marks it rejected (and raises), not answering leaves it pending (and raises).

        With `claim`, the entry is claimed first (see the module doc); False means another sender has it or sent it.
        Without, False means the version is still running."""
        claimed = None
        if claim:
            claimed = self.outbox.claim(record["uid"], self.timeout)
            if claimed is None:
                return False
            current = self.outbox.load(record["uid"])  # read again: another sender may have sent it meanwhile
            if current is None or current["sync"] not in ("pending", "running"):
                self.outbox.release(claimed)
                return False
            record.clear()
            record.update(current)
        try:
            return self.push(record, claimed)
        except Unreachable:
            raise
        except (Unauthorized, Forbidden) as error:  # the token, not the entry: it waits until the token is right
            raise Waiting(f"{error}; {record['name']}@{record['uid']} waits") from error
        except StoreError as error:
            if record["status"] != "running" and self._final_there(record):
                return self._done(record)
            record.update(sync="rejected", error=str(error))
            self.outbox.save(record)
            raise
        except (
            OSError
        ) as error:  # a file of this repo (a blob being read): not the remote's verdict; try again next time
            raise NotRecorded(f"{record['name']}@{record['uid']}: {error}") from error
        finally:
            self.outbox.release(claimed)

    def flush(self, deadline: float | None = None) -> dict:
        """Send every pending entry in uid order; stops at the first one the remote does not answer for, and (`deadline`,
        a time.monotonic() value) once that time has passed: what is left waits for the next sync.

        First the repo's own earlier versions are adopted (see `adopt`). A running entry whose owner is gone
        (`Outbox.dead`) is failed first. Returns a report: {"sent": [entries], "rejected": [entries], "waiting":
        [entries still pending or running], "failed": [entries failed here], "adopted": count, "lines": [what happened]}.
        """
        result = {"sent": [], "rejected": [], "waiting": [], "failed": [], "adopted": 0, "lines": []}
        say = result["lines"].append
        try:
            result["adopted"] = self.adopt()
        except Unreachable:
            result["waiting"] = self.pending()
            return result
        if result["adopted"]:
            say(f"adopted {result['adopted']} versions this repo numbered itself (the remote renumbers them)")
        records = self.ordered(self.pending())
        for index, record in enumerate(records):
            if deadline is not None and time.monotonic() > deadline:
                left = [record for record in records[index:] if record not in result["waiting"]]
                result["waiting"].extend(left)
                say(f"{len(left)} versions left for the next sync (out of time)")
                break
            if record["sync"] == "running":
                why = self.outbox.dead(record, self.timeout)
                if why is None:
                    result["waiting"].append(record)  # its own process sends it
                    continue
                claimed = self.outbox.claim(record["uid"], self.timeout)  # its owner may be saving it right now
                if claimed is None:
                    continue
                try:
                    current = self.outbox.load(record["uid"])
                    if (
                        current is None
                        or current["sync"] != "running"
                        or self.outbox.dead(current, self.timeout) is None
                    ):
                        continue
                    record = current
                    record.update(
                        sync="pending", status="failed", error=f"failed by {hostname()}: {why}", completed=now()
                    )
                    self.outbox.save(record)  # a queued failure, so nothing stays running for good
                finally:
                    self.outbox.release(claimed)
                result["failed"].append(record)
                say(f"failing {record['name']} ({refs.default_short_id(record['uid'])}): {why}")
            try:
                sent = self.send(record)
            except (NotRecorded, Waiting) as error:  # this entry waits; the others need not
                result["waiting"].append(record)
                say(f"waiting: {error}")
                continue
            except Unreachable:
                result["waiting"].extend(record for record in records[index:] if record not in result["waiting"])
                break
            except StoreError as error:
                result["rejected"].append(record)
                say(f"rejected {record['name']} ({refs.default_short_id(record['uid'])}): {error}")
                continue
            if not sent:
                if self.outbox.load(record["uid"]) is not None:
                    result["waiting"].append(record)  # another sender has it
                continue
            result["sent"].append(record)
            what = "sent failed" if record["status"] == "failed" else "sent"
            say(f"{what} {record['name']}@v{record['number']} ({refs.default_short_id(record['uid'])})")
        return result

    def adopt(self) -> int:
        """Hand the versions this repo numbered itself (before it had a remote) to the outbox, oldest first: completed
        ones to publish, failed ones to record as failed; their inputs named by uid, their aliases (other than
        `latest`) along. They leave the repo's database once their outbox entries exist, and come back as copies,
        numbered by the remote. Running ones wait until they end; tombstones are dropped (their files are gone).
        Raises Unreachable while the remote does not answer. Returns how many were adopted."""
        uid = self.check_remote()
        rows = self.copies.local_versions()
        if not rows:
            if self.copies.meta("remote") != self._bound(uid):
                self.copies.set_meta("remote", self._bound(uid))
            return 0
        adopted, waiting = [], 0
        rows = sorted(rows, key=lambda row: (row[1].get("completed") or row[1].get("created") or "", row[1]["version"]))
        for name, state in rows:
            if state["status"] == "running":
                waiting += 1
                continue
            adopted.append(state["uid"])
            if state["status"] == "deleted" or self.outbox.folder_of(state["uid"]).exists():
                continue
            aliases = [
                alias
                for alias, number in self.copies.aliases(name).items()
                if number == state["version"] and alias != "latest"
            ]
            failed = state["status"] == "failed"
            record = {
                "uid": state["uid"],
                "kind": "running" if failed else "publish",
                "sync": "pending",
                "status": state["status"],
                "url": self.remote.url,
                "store": uid,
                "name": name,
                "label": state.get("label"),
                "metadata": state.get("metadata") or {},
                "inputs": self._adopted_inputs(state.get("inputs") or []),
                "aliases": aliases,
                "source": state.get("source"),
                "files": state.get("files") or [],
                "created": state.get("created") or now(),
                "completed": state.get("completed"),
                "number": None,
                "pushed": {},
                "owner": {"host": hostname(), "pid": os.getpid()},
                "error": None,
                "adopted": state["version"],
            }
            if not failed:
                record["digest"] = manifest_digest(record["files"])
            self.outbox.create(record, running=False)
        self.copies.forget_versions(adopted)
        if not waiting:
            self.copies.set_meta("remote", self._bound(uid))
        return len(adopted)

    def _adopted_inputs(self, inputs: list[str]) -> list[str]:
        """An adopted version's inputs, named by uid when this repo numbered them; deleted ones are left out."""
        found = []
        for text in inputs:
            state = None
            try:
                ref = refs.parse(text)
                if ref.exact:
                    state = self.copies.get_version(ref.name, ref.version if ref.version is not None else ref.uid)
            except (StoreError, ValueError):
                state = None
            if state is not None and not state.get("origin"):
                if state["status"] == "deleted":
                    continue
                text = str(Ref(ref.name, state["uid"]))
            found.append(text)
        return found

    def refresh(self) -> int:
        """Mirror the remote's aliases of every artifact the repo has versions of (for resolving offline); returns how many."""
        artifacts = {record["name"] for record in self.records()}
        artifacts |= {name for name, _ in self.copies.copies()}
        count = 0
        for name in sorted(artifacts):
            try:
                aliases = self.remote.aliases(name)
            except NotFound:
                continue
            self.mirror(name, aliases)
            count += 1
        return count
