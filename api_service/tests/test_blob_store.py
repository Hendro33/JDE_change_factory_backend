"""
Uploaded files go to the configured blob store: a local folder, or Azure
Blob Storage (several app instances share it). The Azure SDK's container
client is replaced by a recording double at that boundary.
"""

from __future__ import annotations

import pytest


class _FakeContainer:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def upload_blob(self, name, data, overwrite=False):
        assert overwrite
        self.blobs[name] = bytes(data)

    def download_blob(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        data = self.blobs[name]
        return type("D", (), {"readall": lambda self: data})()

    def get_blob_client(self, name):
        blobs = self.blobs
        return type("B", (), {"exists": lambda self: name in blobs})()

    def list_blobs(self, name_starts_with=""):
        return [type("I", (), {"name": n})() for n in list(self.blobs) if n.startswith(name_starts_with)]

    def delete_blob(self, name):
        self.blobs.pop(name, None)


def test_keys_are_plain_relative_paths(isolated_dirs):
    from jde_api_service.persistence import blob_store

    store = blob_store.default()
    for bad in ("../x", "/etc/passwd", "a/../../b", "a//b", ""):
        with pytest.raises(blob_store.BlobStoreError):
            store.put(bad, b"x")


def test_artifacts_and_attachments_use_the_azure_container_when_configured(client, monkeypatch):
    pytest.importorskip("azure.storage.blob")
    from jde_api_service.persistence import blob_store

    from ._discovery import upload_artifact

    fake = _FakeContainer()
    monkeypatch.setattr(blob_store, "_azure", blob_store.AzureBlobStore(fake))
    monkeypatch.setenv("JDE_BLOB_CONTAINER_URL", "https://jadestore.blob.core.windows.net/jade")
    art = upload_artifact(client, "vdb")
    key = f"artifacts/vdb/{art['sha256']}"
    assert key in fake.blobs
    from jde_api_service.discovery import artifacts

    assert artifacts.read_text("vdb", artifacts.get("vdb", art["artifactId"])).startswith("/* Custom credit check */")
    # Nothing was written to the local data folder.
    import os

    from jde_api_service.config import settings

    assert not os.path.exists(os.path.join(settings.data_dir, "artifacts"))
