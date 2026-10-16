"""`.cairn-inputs.jsonl`: every row names a version (a whole id, a 0.14 uid, or a reference); a row that does not is an
error naming the file and the line, and a publish that reads the file fails with it. Nothing is dropped silently."""

import json

import pytest

import cairn
from cairn.store import InvalidRef, read_inputs
from cairn.store.upgrade import legacy_id

from test_store import env, folder  # noqa: F401

ID = "ab" * 32
UID = "01a144ef-bd78-72e6-a221-1439c21f9ea2"


def inputs_folder(tmp_path, *lines: str):
    root = folder(tmp_path, "made", {"x.txt": "x"})
    (root / ".cairn-inputs.jsonl").write_text("".join(line + "\n" for line in lines))
    return root


def test_good_rows_are_read_in_order_once(tmp_path):
    root = inputs_folder(tmp_path, json.dumps({"id": ID}), "", json.dumps({"uid": UID}), json.dumps({"ref": "data@v2"}))
    (root / "sub").mkdir()
    (root / "sub" / ".cairn-inputs.jsonl").write_text(json.dumps({"id": ID, "ref": "data@v1"}) + "\n")
    assert read_inputs(root) == [ID, legacy_id(UID), "data@v2"]


@pytest.mark.parametrize(
    "row, why",
    [
        (json.dumps({"id": ID[:7]}), "not a whole id"),  # a short id
        (json.dumps({"id": ID.upper()}), "not a whole id"),
        (json.dumps({"id": 12}), "not a whole id"),
        (json.dumps({"uid": "nope"}), "not a Cairn 0.14 uid"),
        (json.dumps({"ref": "data@v01"}), "leading zeros"),
        (json.dumps({"ref": 3}), "not text"),
        (json.dumps({"time": "2026-01-01"}), "names no version"),  # missing fields
        (json.dumps(["id", ID]), "JSON object"),
        ('{"id": "' + ID, "not a JSON row"),  # bad JSON (a torn line too)
    ],
)
def test_a_bad_row_is_an_error_naming_the_file_and_the_line(tmp_path, row, why):
    root = inputs_folder(tmp_path, json.dumps({"id": ID}), row)
    with pytest.raises(InvalidRef, match=why) as refused:
        read_inputs(root)
    assert f"{root / '.cairn-inputs.jsonl'}:2:" in str(refused.value)


def test_a_publish_that_reads_a_bad_inputs_file_fails_with_its_line(env, tmp_path):
    root = inputs_folder(tmp_path, json.dumps({"id": "abcd123"}))
    with pytest.raises(InvalidRef, match=r"\.cairn-inputs\.jsonl:1: 'abcd123' is not a whole id"):
        cairn.connect().artifact("made").publish(root)
    assert cairn.connect().artifact("made").versions() == []
