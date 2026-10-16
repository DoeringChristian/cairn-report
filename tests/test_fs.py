"""Network filesystems: detecting them, `cairn server` refusing a root on one, and the LocalRepo warning."""

import pytest

import cairn.server.app as app_module
import cairn.store.local as local_module
from cairn.cli import main
from cairn.server import make_server
from cairn.store import LocalRepo, StoreError
from cairn.store.fs import is_network_type, network_filesystem, type_from_mounts
from cairn.store.tokens import init_root
from conftest import write_config

MOUNTS = r"""sysfs /sys sysfs rw,nosuid 0 0
/dev/nvme0n1p2 / ext4 rw,relatime 0 0
fileserver:/export/home /home nfs4 rw,vers=4.2 0 0
/dev/sdb1 /home/alice/local xfs rw 0 0
ceph-fuse /ceph fuse.ceph-fuse rw 0 0
10.0.0.1@tcp:/scratch /scratch lustre rw 0 0
/dev/gpfs0 /gpfs gpfs rw 0 0
//nas/share /mnt/nas cifs rw 0 0
beegfs_nodev /beegfs beegfs rw 0 0
/dev/sdc /mnt/with\040space ext4 rw 0 0
tmpfs /tmp tmpfs rw 0 0
fileserver:/export/old /data nfs rw 0 0
/dev/sdd /data btrfs rw 0 0
"""


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/home/bob/store", "nfs4"),
        ("/home", "nfs4"),
        ("/home/alice/local/store", "xfs"),  # a local disk mounted inside the NFS home
        ("/homework/store", "ext4"),  # a prefix of the name is not a parent folder
        ("/ceph/projects/store", "fuse.ceph-fuse"),
        ("/scratch/u/store", "lustre"),
        ("/gpfs/x", "gpfs"),
        ("/mnt/nas/x", "cifs"),
        ("/beegfs/x", "beegfs"),
        ("/mnt/with space/store", "ext4"),  # /proc/mounts escapes spaces as \040
        ("/tmp/store", "tmpfs"),
        ("/data/store", "btrfs"),  # mounted over the NFS mount: the later line wins
        ("/var/lib/store", "ext4"),
    ],
)
def test_the_mount_holding_a_path(path, expected):
    assert type_from_mounts(path, MOUNTS) == expected


@pytest.mark.parametrize(
    "fstype,network",
    [
        ("nfs", True),
        ("nfs4", True),
        ("lustre", True),
        ("gpfs", True),
        ("smbfs", True),
        ("cifs", True),
        ("smb3", True),
        ("beegfs", True),
        ("ceph", True),
        ("fuse.ceph-fuse", True),
        ("fuse.sshfs", True),
        ("afpfs", True),
        ("webdav", True),
        ("ext4", False),
        ("xfs", False),
        ("btrfs", False),
        ("apfs", False),
        ("zfs", False),
        ("tmpfs", False),
        ("fuse.portal", False),
        ("hfs", False),
        ("NFS", True),
    ],
)
def test_network_types(fstype, network):
    assert is_network_type(fstype) is network


def test_this_machines_temporary_folder_is_local(tmp_path):
    assert network_filesystem(tmp_path) is None
    assert network_filesystem(tmp_path / "does" / "not" / "exist") is None  # looked up through its parent


@pytest.fixture
def on_nfs(monkeypatch):
    """Every path looks like it lies on NFS."""
    import cairn.client.open as open_module

    monkeypatch.setattr(local_module, "network_filesystem", lambda path: "nfs4")
    monkeypatch.setattr(open_module, "network_filesystem", lambda path: "nfs4")


def test_the_server_refuses_a_repo_on_a_network_filesystem(tmp_path, on_nfs, capsys):
    root = tmp_path / "root"
    init_root(root)
    with pytest.raises(SystemExit) as refused:
        make_server(root, port=0)
    assert "network filesystem (nfs4)" in str(refused.value) and "allow_network_fs" in str(refused.value)
    server = make_server(root, port=0, allow_network_fs=True)  # the only writer: allowed
    server.server_close()
    with pytest.raises(SystemExit):
        app_module.serve_store(tmp_path / "new-root", port=0)
    assert not (tmp_path / "new-root").exists()  # refused before anything was created


def test_the_cli_passes_allow_network_fs(tmp_path, monkeypatch):
    import cairn.server

    seen = {}
    monkeypatch.setattr(cairn.server, "serve_store", lambda root, **options: seen.update(options))
    main(["server", str(tmp_path / "r"), "--no-ui", "--allow-network-fs"])
    assert seen["allow_network_fs"] is True
    main(["server", str(tmp_path / "r"), "--no-ui"])
    assert seen["allow_network_fs"] is False


def test_a_repo_database_is_never_made_on_a_network_filesystem(tmp_path, on_nfs):
    with pytest.raises(StoreError, match=r"network filesystem \(nfs4\).*cairn remote set.*allow_network_fs"):
        LocalRepo(tmp_path / "shared")
    assert not (tmp_path / "shared" / "metadata.sqlite3").exists()
    LocalRepo(tmp_path / "allowed", allow_network_fs=True).create_project("p")  # one writer, by the owner's word


def test_without_a_remote_a_repo_on_a_network_filesystem_explains_the_cluster_setup(
    tmp_path, on_nfs, monkeypatch, capsys
):
    from cairn.client.open import open_repo
    from cairn.config import load_config

    write_config(tmp_path, "")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(StoreError, match=r"cairn server.*cairn remote set.*cairn sync.*allow_network_fs"):
        open_repo(load_config(tmp_path))
    with pytest.raises(SystemExit, match="network filesystem"):
        main(["versions", "x"])
    assert not (tmp_path / ".cairn" / "metadata.sqlite3").exists()


def test_allow_network_fs_comes_from_cairn_toml(tmp_path, on_nfs, monkeypatch):
    from cairn.client.open import open_repo
    from cairn.config import load_config

    write_config(tmp_path, "[repo]\nallow_network_fs = true\n")
    assert open_repo(load_config(tmp_path)).summaries() == []
    write_config(tmp_path, '[repo]\nallow_network_fs = "yes"\n')
    with pytest.raises(ValueError, match="allow_network_fs must be true or false"):
        load_config(tmp_path)
