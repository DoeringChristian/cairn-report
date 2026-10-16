"""A blob upload that the server answers with a 5xx is a remote that does not answer now (the version waits, pending),
never a refusal for good: a run's draft is not failed and an outbox entry is not rejected because the server's disk
was full for a moment."""

import pytest

from cairn.client import remote as remote_module
from cairn.client.http import ClientError
from cairn.store.errors import StoreError, Unreachable


class FakeClient:
    url = "http://example.org"
    token = None


@pytest.mark.parametrize("status", [500, 507, 503])
def test_a_5xx_blob_upload_is_unreachable_not_a_refusal(monkeypatch, tmp_path, status):
    def failing(client, sources, cache, errors=None, quiet=False):
        errors.append(ClientError(f"PUT /api/v3/blobs/x: HTTP {status}", status))
        return set(sources), 0

    monkeypatch.setattr(remote_module, "upload_missing", failing)
    target = remote_module.Remote(FakeClient(), "study")
    with pytest.raises(Unreachable):
        target.upload({"a" * 64: (tmp_path / "f", 1)}, None, quiet=True)


def test_a_4xx_blob_upload_stays_a_refusal(monkeypatch, tmp_path):
    def failing(client, sources, cache, errors=None, quiet=False):
        errors.append(ClientError("PUT /api/v3/blobs/x: HTTP 413", 413))
        return set(sources), 0

    monkeypatch.setattr(remote_module, "upload_missing", failing)
    with pytest.raises(StoreError) as caught:
        remote_module.Remote(FakeClient(), "study").upload({"a" * 64: (tmp_path / "f", 1)}, None, quiet=True)
    assert not isinstance(caught.value, Unreachable)
