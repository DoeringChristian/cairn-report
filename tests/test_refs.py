"""The one reference syntax (store/refs.py), short ids, the invariants of identity and numbering (docs/architecture.md),
and every entry point resolving the same reference the same way. Every id a case depends on is constructed
(`ids.chosen`): nothing here relies on random ids to hit a case."""

import io
import json
import threading
from pathlib import Path

import pytest

import cairn
from cairn import LocalRepo
from cairn.cli import main
from cairn.store import Ambiguous, InvalidRef, NotFound, StoreError, refs
from cairn.store.ids import ID, chosen, new_id

from conftest import write, write_config
from test_store import env  # noqa: F401

DIGITS = "1234567890" * 6 + "1234"  # an id of decimal digits only: never a version number
ZEROS = "0000abc" + "d" * 57
CAFE = "cafe123" + "e" * 57
TWIN_A = "abcdef1" + "0" + "5" * 56  # two ids that share their first 7 digits
TWIN_B = "abcdef1" + "1" + "5" * 56
LONELY = "f" * 64


def entries(local, files):
    import hashlib

    found = []
    for path, text in files.items():
        data = text.encode()
        sha = hashlib.sha256(data).hexdigest()
        local.put_blob(sha, io.BytesIO(data), len(data))
        found.append({"path": path, "sha256": sha, "size": len(data)})
    return found


# --------------------------------------------------------------------- the parser


@pytest.mark.parametrize(
    "text,form",
    [
        ("1234", ("id", "1234")),
        ("3f9a2c1", ("id", "3f9a2c1")),
        ("DEADBEEF", ("id", "deadbeef")),
        (DIGITS, ("id", DIGITS)),
        ("0000", ("id", "0000")),
        ("data", ("name", "data", None)),
        ("runs/x", ("name", "runs/x", None)),
        ("abc", ("name", "abc", None)),  # three hex digits are a name: an id has at least 4
        ("v2-data", ("name", "v2-data", None)),
        ("data@v3", ("name", "data", "v3")),
        ("data@best", ("name", "data", "best")),
        ("data@run-2026", ("name", "data", "run-2026")),
        ("runs/x@2026-10-16_12-00-00/3", ("name", "runs/x", "2026-10-16_12-00-00/3")),
        ("data@latest", ("name", "data", "latest")),
    ],
)
def test_every_form_by_its_shape(text, form):
    ref = refs.parse(text)
    if form[0] == "id":
        assert (ref.id, ref.name, ref.selector) == (form[1], None, None)
    else:
        assert (ref.id, ref.name, ref.selector) == (None, form[1], form[2])
    with_path = refs.parse(f"{text}//figures/a.png")
    assert with_path.path == "figures/a.png" and with_path.at(None) == ref
    assert str(refs.parse(str(ref))) == str(ref)


def test_the_forms_never_overlap():
    assert refs.parse("data@v3").version == 3 and refs.parse("data@v3").word is None
    assert refs.parse("data@best").word == "best" and refs.parse("data@best").version is None
    assert refs.parse("data").word == "latest"
    assert refs.parse(DIGITS).whole and refs.parse(DIGITS).exact and not refs.parse("1234567").exact
    for text in ("data@3f9a2c1", "data@1234", "data@CAFE", "cafe@v1", "1234@best"):  # an id never has an `@`
        with pytest.raises(InvalidRef):
            refs.parse(text)
    with pytest.raises(InvalidRef, match="write 3f9a2c1"):
        refs.parse("data@3f9a2c1")
    for text in ("data@v03", "data@", "a" * 65 + "b", "f" * 65, "p:data@v1", " data", "data//", "data//../x"):
        with pytest.raises(InvalidRef):
            refs.parse(text)


@pytest.mark.parametrize("word", ["cafe", "1234", "DEADBEEF", "0000", "Cafe1234", "ffffffff"])
def test_names_aliases_and_labels_of_hex_digits_alone_are_refused(word, tmp_path):
    with pytest.raises(InvalidRef):
        refs.valid_name(word.lower())
    with pytest.raises(InvalidRef):
        refs.new_alias(word)
    with pytest.raises(InvalidRef):
        refs.new_label(word)
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    with pytest.raises(InvalidRef):
        local.publish("p", word.lower(), entries(local, {"a.txt": "1"}))
    number = local.publish("p", "data", entries(local, {"a.txt": "1"}))
    with pytest.raises(InvalidRef):
        local.set_alias("p", "data", word, number)
    with pytest.raises(InvalidRef):
        local.publish("p", "data", entries(local, {"a.txt": "2"}), label=word)
    with pytest.raises(InvalidRef):
        local.start_version("p", "data", label=word)


@pytest.mark.parametrize("name", ["data", "runs/x", "abc", "v2-data", "runs/cafe", "cafe-1", "beef/1234"])
def test_names_that_are_not_hex_alone_are_allowed(name, tmp_path):
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    assert local.publish("p", name, entries(local, {"a.txt": "1"})) == 1
    assert local.resolve("p", name)[0] == name


def test_refused_at_every_place_a_name_alias_or_label_is_made(env):
    data = write(env / "out" / "a.txt", "1").parent
    for call in (
        lambda: cairn.publish("cafe", data),
        lambda: cairn.publish("data", data, aliases=["1234"]),
        lambda: cairn.publish("data", data, label="DEADBEEF"),
        lambda: cairn.draft("0000"),
        lambda: cairn.draft("data", label="cafe"),
        lambda: cairn.draft("data", aliases=["beef"]),
        lambda: cairn.init(name="cafe"),
    ):
        with pytest.raises(InvalidRef):
            call()
    cairn.publish("data", data)
    with pytest.raises(InvalidRef):
        cairn.alias("data@v1", "1234")
    with pytest.raises(SystemExit):
        main(["alias", "data@v1", "CAFE"])


def test_an_alias_and_a_label_never_share_a_word(tmp_path):
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    v1 = local.publish("p", "data", entries(local, {"a.txt": "1"}), label="run-2026")
    with pytest.raises(InvalidRef, match="label"):
        local.set_alias("p", "data", "run-2026", v1)
    local.set_alias("p", "data", "best", v1)
    with pytest.raises(InvalidRef, match="alias"):
        local.publish("p", "data", entries(local, {"a.txt": "2"}), label="best")
    with pytest.raises(InvalidRef):
        local.publish("p", "data", entries(local, {"a.txt": "3"}), label="latest")
    with pytest.raises(InvalidRef):  # in one publish too
        local.publish("p", "other", entries(local, {"a.txt": "4"}), label="paper", aliases=["paper"])
    assert local.resolve("p", "data@run-2026")[1]["version"] == v1 == local.resolve("p", "data@best")[1]["version"]


# --------------------------------------------------------------------- short ids


@pytest.fixture
def ids_repo(tmp_path):
    """A repo whose versions have constructed ids: data v1 (all digits), v2 (0000abc...), models v1 (cafe123...),
    twins v1, v2 (sharing 7 digits), and in another project a version whose id is TWIN_A's neighbour."""
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    with chosen(DIGITS, ZEROS, CAFE, TWIN_A, TWIN_B):
        local.publish("p", "data", entries(local, {"a.txt": "1"}))
        local.publish("p", "data", entries(local, {"a.txt": "2"}), label="run-2026")
        local.publish("p", "models", entries(local, {"m.txt": "m"}))
        local.publish("p", "twins", entries(local, {"t.txt": "1"}))
        local.publish("p", "twins", entries(local, {"t.txt": "2"}))
    local.create_project("q")
    local.publish("q", "data", entries(local, {"a.txt": "q"}), id="abcdef1" + "2" + "6" * 56)
    return local


def test_a_short_id_is_at_least_7_digits_and_longer_only_when_needed(ids_repo):
    shorts = ids_repo.short_ids("p", [DIGITS, ZEROS, CAFE, TWIN_A, TWIN_B])
    assert shorts == {DIGITS: "1234567", ZEROS: "0000abc", CAFE: "cafe123", TWIN_A: "abcdef10", TWIN_B: "abcdef11"}
    assert ids_repo.short_ids("q", ["abcdef1" + "2" + "6" * 56])["abcdef1" + "2" + "6" * 56] == "abcdef1"  # per project
    assert ids_repo.short_ids("p", [LONELY]) == {LONELY: "fffffff"}  # an id the project lacks: a prefix none shares


def test_a_short_id_grows_when_a_new_id_shares_its_digits(tmp_path):
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    local.publish("p", "a", entries(local, {"a": "1"}), id=TWIN_A)
    assert local.short_ids("p", [TWIN_A])[TWIN_A] == "abcdef1"
    local.publish("p", "b", entries(local, {"b": "1"}), id="abcdef10" + "9" * 56)  # shares 8 digits with TWIN_A
    assert local.short_ids("p", [TWIN_A])[TWIN_A] == "abcdef105"


@pytest.mark.parametrize("length", [4, 5, 7, 12, 64])
def test_any_unique_prefix_of_4_or_more_digits_names_the_version(ids_repo, length):
    for id, name in ((DIGITS, "data"), (ZEROS, "data"), (CAFE, "models")):
        found, version = ids_repo.find("p", id[:length])
        assert (found, version["id"]) == (name, id)
    assert ids_repo.find("p", TWIN_A[:8])[1]["id"] == TWIN_A


def test_an_ambiguous_prefix_lists_every_version_it_names(ids_repo):
    with pytest.raises(Ambiguous) as caught:
        ids_repo.find("p", "abcdef1")
    message = str(caught.value)
    assert "twins@v1 (abcdef10)" in message and "twins@v2 (abcdef11)" in message and "give more digits" in message
    assert {item["id"] for item in caught.value.candidates} == {TWIN_A, TWIN_B}
    with pytest.raises(NotFound):
        ids_repo.find("p", "9999")
    assert ids_repo.find("q", "abcdef1")[0] == "data"  # the other project's own id: no ambiguity across projects


# --------------------------------------------------------------------- the invariants


def test_invariant_1_ids_are_random_256_bit_and_never_collide():
    made = {new_id() for _ in range(20_000)}
    assert len(made) == 20_000 and all(ID.fullmatch(id) for id in made)
    assert len({id[:16] for id in made}) == 20_000  # no structure: even their first 64 bits differ
    with chosen(DIGITS):  # the test hook hands out chosen ids first
        assert new_id() == DIGITS and new_id() != DIGITS


def test_invariant_2_only_the_source_of_truth_numbers_versions(down, env):
    """A version made while the remote is down has an id and no number; the repo never numbers it."""
    data = write(env / "out" / "a.txt", "1").parent
    pending = cairn.connect().artifact("data").publish(data)
    assert pending.number is None and ID.fullmatch(pending.id)
    assert cairn.connect().version(pending.id).number is None


def test_invariant_3_latest_is_the_highest_completed_number_not_the_last_to_finish(tmp_path):
    local = LocalRepo(tmp_path / "repo")
    local.create_project("p")
    first = local.start_version("p", "run")  # v1 starts first
    second = local.start_version("p", "run")  # v2 starts later
    local.add_files("p", "run", second, entries(local, {"a": "2"}))
    local.complete_version("p", "run", second)
    local.add_files("p", "run", first, entries(local, {"a": "1"}))
    local.complete_version("p", "run", first)  # v1 completes last: it does not take `latest` from v2
    assert local.resolve("p", "run")[1]["version"] == 2 == local.aliases("p", "run")["latest"]
    local.delete_version("p", "run", 2, force=True)
    assert local.resolve("p", "run")[1]["version"] == 1  # the highest completed number left


def test_invariant_3_a_pending_version_is_latest_because_it_arrives_last(down, env):
    """With a remote: the remote's latest, unless this repo has a pending version (it will arrive after every version
    the remote has, so it gets the highest number); among pending ones, the one sent last. Never a clock."""
    store = cairn.connect()
    first = store.artifact("data").publish(write(env / "a" / "x.txt", "1").parent)
    second = store.artifact("data").publish(write(env / "b" / "x.txt", "2").parent)
    assert store.version("data").id == second.id != first.id
    order = [record["id"] for record in store.backend.engine.ordered(store.backend.pending())]
    assert order == [first.id, second.id]


def test_invariant_4_stored_references_are_whole_ids(tmp_path, env):
    """Inputs, the inputs file, lineage, aliases, and a report's manifest store whole ids, never a short id or a
    number alone, whatever form the reference was given in."""
    data = write(env / "d" / "x.txt", "1").parent
    with chosen(DIGITS):
        source = cairn.publish("data", data)
    cairn.alias(f"data@v{source}", "best")
    out = write(env / "o" / "y.txt", "1").parent
    cairn.connect().artifact("result").publish(out, inputs=["1234567", "data@best", "data@v1", "data"])
    result = cairn.connect().version("result")
    assert result.inputs == [DIGITS] and result.manifest["inputs"] == [DIGITS]
    assert cairn.connect().version("data@v1").consumers == [result.id]
    cairn.connect().pull("1234567")  # outside a run: the working directory's inputs file, by the whole id
    rows = [json.loads(line) for line in Path(".cairn-inputs.jsonl").read_text().splitlines()]
    assert rows[-1]["id"] == DIGITS
    local = LocalRepo(env / ".cairn")
    with local._db.read() as connection:
        stored = connection.execute("SELECT id FROM aliases WHERE alias = 'best'").fetchone()[0]
        edges = connection.execute("SELECT input, consumer FROM consumers").fetchall()
    assert stored == DIGITS and edges == [(DIGITS, result.id)]


def test_invariant_5_no_form_is_read_as_another_when_it_misses(ids_repo):
    """A name that does not exist is NotFound, never tried as an id; an id that matches nothing is NotFound, never
    tried as a name; `data@1234567` is refused, not looked up as a label and then as an id."""
    with pytest.raises(NotFound):
        ids_repo.find("p", "abc")  # three hex digits: a name, and no artifact has it
    with pytest.raises(NotFound, match="no version with the id 9999"):
        ids_repo.find("p", "9999")
    with pytest.raises(InvalidRef):
        ids_repo.find("p", "data@1234567")
    assert ids_repo.find("p", "1234567")[1]["id"] == DIGITS  # all digits: an id, not v1234567


def test_invariant_6_only_refs_parses_references():
    """See tests/test_boundaries.py (test_only_refs_takes_a_reference_apart and its web twin)."""
    import test_boundaries

    test_boundaries.test_only_refs_takes_a_reference_apart()
    test_boundaries.test_only_one_web_module_takes_a_reference_apart()


def test_invariant_7_aliases_and_deletions_change_only_on_the_source_of_truth(down, env):
    store = cairn.connect()
    pending = store.artifact("data").publish(write(env / "a" / "x.txt", "1").parent)
    with pytest.raises(StoreError, match="pending"):
        store.artifact("data").alias("best", pending)
    with pytest.raises(StoreError, match="pending"):
        pending.delete()
    with pytest.raises(StoreError, match="needs the remote"):
        store.backend.set_alias("data", "best", 1)
    with pytest.raises(StoreError, match="needs the remote"):
        store.backend.delete_version("data", 1)


# --------------------------------------------------------------------- every entry point, the same answer


FORMS = {  # reference -> the id it names
    "1234567": DIGITS,
    DIGITS: DIGITS,
    "0000abc": ZEROS,
    "cafe123": CAFE,
    "CAFE123E": CAFE,
    "data": ZEROS,  # latest: v2
    "data@v1": DIGITS,
    "data@run-2026": ZEROS,
    "data@best": DIGITS,
    "abcdef10": TWIN_A,
    "abcdef11": TWIN_B,
}
AMBIGUOUS = "abcdef1"


@pytest.fixture
def entry_points(env, monkeypatch):
    """The project `study` with constructed ids, served by `cairn server` (its own repo) and a `cairn ui` over it."""
    from cairn.server import make_server
    from cairn.store.tokens import init_root
    from cairn.ui.server import LocalBackend, make_ui_server

    local = LocalRepo(env / ".cairn")
    local.create_project("")
    with chosen(DIGITS, ZEROS, CAFE, TWIN_A, TWIN_B):
        cairn.publish("data", write(env / "d1" / "a.txt", "1").parent)
        cairn.publish("data", write(env / "d2" / "a.txt", "2").parent, label="run-2026")
        cairn.publish("models", write(env / "m" / "m.txt", "m").parent)
        cairn.publish("twins", write(env / "t1" / "t.txt", "1").parent)
        cairn.publish("twins", write(env / "t2" / "t.txt", "2").parent)
    cairn.alias("data@v1", "best")
    init_root(env / ".cairn")
    token = (env / ".cairn" / "token").read_text().strip()
    server = make_server(env / ".cairn", port=0, own="study")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ui = make_ui_server(LocalBackend(env / ".cairn", no_auth=True), port=0, no_auth=True)
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    yield {"server": server.server_address[1], "token": token, "ui": ui.server_address[1], "env": env}
    ui.shutdown()
    ui.server_close()
    server.shutdown()
    server.server_close()


def _get(port, path, headers):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port)
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, json.loads(body or b"null")


def resolved_everywhere(points, ref: str, capsys) -> dict:
    """The id every entry point answers for `ref` (or the error's class name)."""
    from urllib.parse import quote

    from cairn.reports.build import build_all
    from cairn.config import load_config

    found = {"parse": str(refs.parse(ref))}
    try:
        found["python"] = cairn.connect().version(ref).id
    except StoreError as error:
        found["python"] = type(error).__name__
    capsys.readouterr()
    try:
        main(["show", ref])
        found["cli"] = next(line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("id "))
    except SystemExit as exit:
        found["cli"] = "Ambiguous" if "names 2 versions" in str(exit.code) else str(exit.code)
    status, answer = _get(
        points["server"],
        f"/api/v3/projects/study/find?ref={quote(ref)}",
        {"Authorization": f"Bearer {points['token']}"},
    )
    found["server"] = answer["version"]["id"] if status == 200 else ("Ambiguous" if status == 409 else status)
    status, answer = _get(points["ui"], f"/ui-api/projects/study/version?ref={quote(ref)}", {"X-Cairn": "1"})
    found["ui"] = answer["version"]["id"] if status == 200 else ("Ambiguous" if status == 409 else status)
    report = points["env"] / "reports" / "pin"
    write(report / "report.md", f"---\ntitle: Pin\ninputs:\n  pinned: '{ref}'\n---\n\nText.\n")
    try:
        found["report"] = build_all(load_config(points["env"]), report="pin")[0].inputs["pinned"]["id"]
    except Exception as error:  # noqa: BLE001 - a BuildError carries the store's message
        found["report"] = "Ambiguous" if "names 2 versions" in str(error) else type(error).__name__
    capsys.readouterr()
    return found


@pytest.mark.parametrize("ref", list(FORMS))
def test_every_entry_point_resolves_a_reference_to_the_same_id(entry_points, ref, capsys):
    found = resolved_everywhere(entry_points, ref, capsys)
    expected = FORMS[ref]
    assert {key: value for key, value in found.items() if key != "parse"} == {
        key: expected for key in ("python", "cli", "server", "ui", "report")
    }, found


def test_every_entry_point_refuses_an_ambiguous_prefix_the_same_way(entry_points, capsys):
    found = resolved_everywhere(entry_points, AMBIGUOUS, capsys)
    assert {key: value for key, value in found.items() if key != "parse"} == {
        key: "Ambiguous" for key in ("python", "cli", "server", "ui", "report")
    }, found


def test_an_unquoted_number_in_report_inputs_is_refused_not_misread(entry_points, capsys):
    from cairn.config import load_config
    from cairn.reports.build import BuildError, build_all

    report = entry_points["env"] / "reports" / "pin"
    write(report / "report.md", "---\ntitle: Pin\ninputs:\n  pinned: 1234567\n---\n\nText.\n")
    with pytest.raises(BuildError, match="quote the reference"):
        build_all(load_config(entry_points["env"]), report="pin")


@pytest.fixture
def down(env):
    """The project `env` with a remote that never answers (a closed port)."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    write_config(env, f'[remote]\nurl = "http://127.0.0.1:{port}/study"\n')
    return env
