"""Where a client looks for its token, first found wins: $CAIRN_TOKEN, [repo] token_file, the repo's `token` file,
~/.config/cairn/token. A refusal names the source used (never the token); no token lists the places looked in."""

import os
import threading
from pathlib import Path

import pytest

import cairn
from cairn.client.open import client, find_token, home_token
from cairn.config import load_config
from cairn.store.tokens import init_root

pytest.importorskip("uvicorn")


def write_token(path: Path, token: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    os.chmod(path, mode)
    return path


@pytest.fixture
def study(tmp_path, monkeypatch):
    folder = tmp_path / "study"
    (folder / ".cairn").mkdir(parents=True)
    monkeypatch.chdir(folder)
    return folder


def configure(folder: Path, token_file: str | None = None, remote: str = "http://127.0.0.1:9/study") -> None:
    lines = ["[repo]", f'remote = "{remote}"'] + ([f'token_file = "{token_file}"'] if token_file else [])
    (folder / "cairn.toml").write_text("\n".join(lines) + "\n")


def test_each_source_shadows_the_later_ones(study, monkeypatch, capsys):
    home = write_token(home_token(), "h" * 40)
    repo = write_token(study / ".cairn" / "token", "r" * 40)
    own = write_token(study / "secrets" / "token", "f" * 40)
    configure(study, "secrets/token")
    monkeypatch.setenv("CAIRN_TOKEN", "e" * 40)
    assert find_token(load_config())[:2] == ("e" * 40, "$CAIRN_TOKEN")
    monkeypatch.delenv("CAIRN_TOKEN")
    assert find_token(load_config())[:2] == ("f" * 40, str(own))
    configure(study)
    assert find_token(load_config())[:2] == ("r" * 40, str(repo))
    repo.unlink()
    assert find_token(load_config())[:2] == ("h" * 40, str(home))
    home.unlink()
    token, source, places = find_token(load_config())
    assert (token, source) == ("", None)
    for place in ("$CAIRN_TOKEN", "[repo] token_file", str(study / ".cairn" / "token"), str(home)):
        assert place in places


def test_a_token_file_readable_by_others_is_used_with_a_warning(study, capsys):
    configure(study)
    write_token(study / ".cairn" / "token", "r" * 40, mode=0o644)
    assert find_token(load_config())[0] == "r" * 40
    assert "readable by other users" in capsys.readouterr().err


@pytest.fixture
def server(tmp_path):
    from cairn.server import make_server

    root = tmp_path / "remote"
    owner = init_root(root).read_text().strip()
    httpd = make_server(root, port=0)
    httpd.log = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", owner
    httpd.shutdown()
    httpd.server_close()


def test_a_refused_token_names_its_source_never_the_token(study, server):
    url, owner = server
    configure(study, remote=f"{url}/study")
    wrong = write_token(study / ".cairn" / "token", "w" * 40)
    with pytest.raises(cairn.Unauthorized) as caught:
        cairn.connect().artifacts()
    assert f"the token from {wrong} was refused by {url}/study" in str(caught.value)
    assert "w" * 40 not in str(caught.value)


def test_no_token_lists_the_places_looked_in(study, server):
    url, _ = server
    configure(study, remote=f"{url}/study")
    with pytest.raises(cairn.Unauthorized) as caught:
        cairn.connect().artifacts()
    message = str(caught.value)
    assert f"{url}/study needs a token, and none was found" in message
    for place in ("$CAIRN_TOKEN", "[repo] token_file", str(study / ".cairn" / "token"), str(home_token())):
        assert place in message


def test_the_repo_token_file_is_enough_for_a_machine(study, server):
    url, owner = server
    configure(study, remote=f"{url}/study")
    write_token(study / ".cairn" / "token", owner)
    data = study / "data"
    data.mkdir()
    (data / "a.txt").write_text("a")
    version = cairn.connect().artifact("data").publish(data)
    assert version.number == 1 and client(load_config()).token_source == str(study / ".cairn" / "token")
