"""
Where Jade keeps uploaded files: JD Edwards source exports and reference
documents, process-framework workbooks, and request attachments with their
extracted text. Everything else is in the database.

One interface, two back-ends, chosen by configuration only:

  * local folder (default): files under <JDE_API_DATA_DIR>/<key>. A
    single-server installation.
  * Azure Blob Storage: set JDE_BLOB_CONTAINER_URL to the container's URL
    (https://<account>.blob.core.windows.net/<container>). The app signs in
    with its managed identity (DefaultAzureCredential); alternatively set
    JDE_BLOB_CONNECTION_STRING (+ JDE_BLOB_CONTAINER) for a storage
    connection string. Several app instances then share the same files.

Keys are relative paths made by Jade itself ("artifacts/<company>/<sha>",
"request_attachments/<company>/<id>/original"); a key never comes from a
user, and anything that is not a plain relative path is refused.
"""

from __future__ import annotations

import os
import re
from typing import Optional, Protocol

from ..config import settings

_KEY = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_.\-]+)*$")


class BlobStoreError(RuntimeError):
    pass


def check_key(key: str) -> str:
    if not _KEY.match(key or "") or ".." in key.split("/"):
        raise BlobStoreError(f"invalid storage key {key!r}")
    return key


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: Optional[str] = None) -> None:
        self.root = root or settings.data_dir

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *check_key(key).split("/"))

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def delete_prefix(self, prefix: str) -> None:
        import shutil

        path = self._path(prefix.rstrip("/"))
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)


class AzureBlobStore:
    """Azure Blob Storage (pip install azure-storage-blob azure-identity)."""

    def __init__(self, container_client) -> None:
        self._c = container_client

    @classmethod
    def from_environment(cls) -> "AzureBlobStore":
        from azure.storage.blob import ContainerClient

        url = os.environ.get("JDE_BLOB_CONTAINER_URL", "").strip()
        if url:
            from azure.identity import DefaultAzureCredential

            return cls(ContainerClient.from_container_url(url, credential=DefaultAzureCredential()))
        conn = os.environ["JDE_BLOB_CONNECTION_STRING"]
        return cls(ContainerClient.from_connection_string(conn, os.environ.get("JDE_BLOB_CONTAINER", "jade")))

    def put(self, key: str, data: bytes) -> None:
        self._c.upload_blob(check_key(key), data, overwrite=True)

    def get(self, key: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._c.download_blob(check_key(key)).readall()
        except ResourceNotFoundError as exc:
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return self._c.get_blob_client(check_key(key)).exists()

    def delete_prefix(self, prefix: str) -> None:
        for blob in self._c.list_blobs(name_starts_with=check_key(prefix.rstrip("/"))):
            self._c.delete_blob(blob.name)


def uses_azure() -> bool:
    return bool(os.environ.get("JDE_BLOB_CONTAINER_URL", "").strip()
                or os.environ.get("JDE_BLOB_CONNECTION_STRING", "").strip())


_azure: Optional[AzureBlobStore] = None


def default() -> BlobStore:
    """The configured store. The local store follows settings.data_dir at
    call time (tests point it at a temporary directory)."""
    global _azure
    if uses_azure():
        if _azure is None:
            _azure = AzureBlobStore.from_environment()
        return _azure
    return LocalBlobStore()
