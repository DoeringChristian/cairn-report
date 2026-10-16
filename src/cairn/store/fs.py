"""Filesystem helpers: copy-on-write clones, and telling a network filesystem from a local one.

A store root holds a SQLite database (db.py) and content-addressed blob files. SQLite's locking (and WAL's
shared memory) and the server's flock are safe between processes on one host with a local filesystem.
Over NFS and other network filesystems they are not: locks are unreliable, WAL needs memory shared by
every reader, and attribute caching hides fresh writes for seconds. So a store root on a network
filesystem has one writer process (`cairn server`) or none: `cairn server` refuses such a root unless told
it is the only process using it (`--allow-network-fs`), and a LocalRepo that writes to one warns once.
(The pull cache is NFS-safe on its own: it only ever writes once and renames, see cache.py.)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import sys
from pathlib import Path

NETWORK_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "nfsd",
    "lustre",
    "gpfs",
    "mmfs",
    "smbfs",
    "smb",
    "smb2",
    "smb3",
    "cifs",
    "beegfs",
    "ceph",
    "afs",
    "openafs",
    "glusterfs",
    "panfs",
    "wekafs",
    "pvfs2",
    "orangefs",
    "afpfs",
    "webdav",
    "davfs",
    "9p",
    "virtiofs",
    "quobyte",
    "cvfs",
}
NETWORK_FUSE = {
    "ceph-fuse",
    "ceph",
    "sshfs",
    "glusterfs",
    "s3fs",
    "rclone",
    "gcsfuse",
    "juicefs",
    "beegfs",
    "lustre",
    "goofys",
    "mountpoint-s3",
    "davfs",
    "curlftpfs",
    "cvmfs",
}


def is_network_type(fstype: str) -> bool:
    """Whether a filesystem type name (as /proc/mounts or statfs names it) is a network filesystem."""
    fstype = fstype.lower()
    if fstype in NETWORK_FILESYSTEMS:
        return True
    if fstype.startswith("fuse.") or fstype.startswith("fuseblk."):
        return fstype.split(".", 1)[1] in NETWORK_FUSE
    return fstype.startswith("nfs")


def _unescape(field: str) -> str:
    """/proc/mounts writes spaces, tabs, newlines and backslashes in paths as octal escapes (`\\040`)."""
    out, index = [], 0
    while index < len(field):
        if field[index] == "\\" and field[index + 1 : index + 4].isdigit():
            out.append(chr(int(field[index + 1 : index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def type_from_mounts(path: str | Path, mounts: str) -> str | None:
    """The filesystem type of the mount holding `path`, from a mount table in /proc/self/mounts format
    (`device mountpoint type options 0 0` per line): the longest mount point that contains the path."""
    target = os.path.normpath(str(path))
    best, found = -1, None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point, fstype = _unescape(fields[1]), fields[2]
        inside = target == point or point == "/" or target.startswith(point.rstrip("/") + "/")
        if inside and len(point) > best:  # a later mount over the same point wins (>= keeps the last)
            best, found = len(point), fstype
        elif inside and len(point) == best:
            found = fstype
    return found


class _StatFS(ctypes.Structure):  # struct statfs on macOS (64-bit inodes) and the BSDs that share its layout
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_type(path: str) -> str | None:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        function = (
            getattr(libc, "statfs$INODE64", None) if sys.maxsize > 2**32 and os.uname().machine == "x86_64" else None
        )
        function = function or libc.statfs
        function.argtypes, function.restype = [ctypes.c_char_p, ctypes.POINTER(_StatFS)], ctypes.c_int
        result = _StatFS()
        if function(os.fsencode(path), ctypes.byref(result)) != 0:
            return None
        return result.f_fstypename.decode(errors="replace") or None
    except (OSError, AttributeError, ValueError):
        return None


def filesystem_type(path: str | Path) -> str | None:
    """The type of the filesystem holding `path` (`apfs`, `ext4`, `nfs4`, `fuse.ceph-fuse`, ...), or None if unknown.
    A path that does not exist yet is looked up through its nearest existing parent."""
    path = Path(path).expanduser().absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    real = os.path.realpath(path)
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as handle:
                return type_from_mounts(real, handle.read())
        except OSError:
            return None
    if sys.platform == "darwin" or "bsd" in sys.platform:
        return _darwin_type(real)
    return None


def network_filesystem(path: str | Path) -> str | None:
    """The filesystem type if `path` lies on a network filesystem, else None."""
    fstype = filesystem_type(path)
    return fstype if fstype and is_network_type(fstype) else None


# ------------------------------------------------------------- clones


def _clonefile():
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        function = libc.clonefile
        function.argtypes, function.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
        return function
    except (OSError, AttributeError):
        return None


_CLONE = _clonefile()
FICLONE = 0x40049409


def clone(source: Path | str, target: Path | str) -> bool:
    """Make `target` (which must not exist) a copy-on-write clone of `source` (APFS `clonefile`, Btrfs/XFS
    `FICLONE`). False, leaving nothing behind, where the filesystem or the pair of paths cannot clone."""
    if _CLONE is not None:
        return _CLONE(os.fsencode(source), os.fsencode(target), 0) == 0
    if sys.platform.startswith("linux"):
        try:
            import fcntl

            with open(source, "rb") as reader, open(target, "xb") as writer:
                fcntl.ioctl(writer.fileno(), FICLONE, reader.fileno())
            return True
        except (OSError, ImportError):
            Path(target).unlink(missing_ok=True)
    return False


def clone_or_copy(source: Path | str, target: Path | str) -> bool:
    """Copy `source` to the new file `target` as a copy-on-write clone where the filesystem has them (APFS
    `clonefile`, Btrfs/XFS `FICLONE`), else as a plain copy. True if it was a clone (costs no space until
    either side changes). `target` must not exist."""
    if clone(source, target):
        return True
    shutil.copyfile(source, target)
    return False


def network_fs_message(root, fstype: str) -> str:
    """Why a repo on a network filesystem has no database, and what to do on a cluster."""
    return (
        f"the repo {root} is on a network filesystem ({fstype}), where SQLite and file locks are unreliable across the "
        "nodes of a cluster, so it cannot hold the repo's database. On a cluster, run `cairn server` on a machine "
        "with a local disk and `cairn remote set https://<server>/<project>` here: jobs then keep each version in its "
        "own outbox folder here and send it to the remote themselves, or `cairn sync` (on the login node) sends "
        "what they left. If one process on one host is the repo's only writer, set [repo] allow_network_fs = true."
    )
