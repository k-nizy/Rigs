"""Where video goes, behind one interface.

The same move the plan makes with RODA-RS, for the same reason: how bulk
bytes actually travel is the one part of this system nobody has answered
yet. Presigned straight to the object store, or streamed through the
gateway? MinIO as a production spool, or only for local development?
Resumable, and whose checksum?

None of that is settled, and all of it lives here. Everything else - the
bookkeeping, the state machine, the rule that lets a rig delete its own
copy - is written against this interface and does not change when the
answer arrives. If it does, the boundary was drawn in the wrong place.

Two implementations:

  LocalStorage  a directory on disk. Real enough to test the whole path
                and honest about being a stand-in.
  MinioStorage  S3, which is what the platform team already runs.

The sizing table in the plan is a guess built on "three 1080p30 cameras
at ~7 Mbps". Every object that lands here is measured, so the moment a
real episode arrives the guess corrects itself instead of waiting for
somebody to measure one by hand.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Upload:
    """Where the rig should put the bytes, and how."""

    url: str
    method: str
    key: str
    # Set when the store issues a real presigned URL. Absent for the
    # local stand-in, where the rig posts to the service instead.
    expires_in_secs: int | None = None


@dataclass(frozen=True)
class Stored:
    key: str
    bytes: int
    sha256: str


class Storage(Protocol):
    """What the rest of the system is allowed to know about storage."""

    def upload_target(self, key: str) -> Upload: ...
    async def head(self, key: str) -> Stored | None: ...
    async def put(self, key: str, data: bytes) -> Stored: ...
    async def copy_to_archive(self, key: str) -> str: ...
    # What the cold tier actually holds. The spool copy is deleted on the
    # strength of this answer, so it has to come from the archive itself
    # rather than from the fact that a copy call returned.
    async def head_archive(self, key: str) -> Stored | None: ...
    async def delete(self, key: str) -> None: ...
    # Separate from delete() on purpose. Deleting from the spool is
    # routine and reversible - the archive still has it. Deleting from
    # the archive is the end of that take, for ever. Two names, so no
    # caller can do the second while meaning the first.
    async def delete_archived(self, key: str) -> None: ...


def object_key(rig_id: str, episode_id: str, camera: str) -> str:
    """One video per camera per episode.

    The episode id is minted on the rig at pedal-press and names the
    directory there too, so the row and the bytes are joined without a
    lookup table - and the id exists even for a take the recorder failed
    to start, which is a row worth having.
    """
    return f"{rig_id}/{episode_id}/{camera}.mp4"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalStorage:
    """A directory. Enough to exercise the whole path without a service.

    Deliberately not pretending to be S3. It has no presigned URLs, so
    the rig uploads through the service; anything that only works because
    of that is a thing to notice now rather than at the swap.
    """

    def __init__(self, root: Path | str, archive: Path | str | None = None) -> None:
        self.root = Path(root)
        self.archive = Path(archive) if archive else self.root / "_archive"
        self.root.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"key escapes the storage root: {key!r}")
        return p

    def upload_target(self, key: str) -> Upload:
        # No presigning here: the service takes the bytes itself.
        return Upload(url=f"/api/storage/{key}", method="PUT", key=key)

    async def put(self, key: str, data: bytes) -> Stored:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return Stored(key=key, bytes=len(data), sha256=sha256_of(data))

    async def head(self, key: str) -> Stored | None:
        p = self._path(key)
        if not p.exists():
            return None
        data = p.read_bytes()
        return Stored(key=key, bytes=len(data), sha256=sha256_of(data))

    async def copy_to_archive(self, key: str) -> str:
        src = self._path(key)
        dst = self.archive / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return str(dst)

    async def head_archive(self, key: str) -> Stored | None:
        p = self.archive / key
        if not p.exists():
            return None
        data = p.read_bytes()
        return Stored(key=key, bytes=len(data), sha256=sha256_of(data))

    async def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    async def delete_archived(self, key: str) -> None:
        p = self.archive / key
        if p.exists():
            p.unlink()


class MinioStorage:
    """S3, which is what the platform team already runs.

    Written against boto3 the way their convention describes - a client
    per process, sized to its own concurrency, rather than a shared global
    pool. Untested against a live bucket until one exists; the interface
    above is what the rest of the system depends on, and swapping this in
    should change nothing outside this class.
    """

    def __init__(self, endpoint: str, bucket: str, access_key: str,
                 secret_key: str, archive_bucket: str | None = None,
                 presign_secs: int = 3600) -> None:
        import boto3  # imported here so LocalStorage needs no dependency

        self.bucket = bucket
        self.archive_bucket = archive_bucket or f"{bucket}-archive"
        self.presign_secs = presign_secs
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def upload_target(self, key: str) -> Upload:
        """A presigned PUT, so the bytes never pass through an application
        worker. At ~2.6 TB a day that is the difference between a storage
        system and an outage.

        `ChecksumSHA256` is part of the signature: the store computes the
        digest as the bytes arrive and refuses the upload if it does not
        match. That matters because with a presigned PUT the server never
        sees the bytes and cannot hash them itself - without this it would
        be taking the rig's word for it, and the whole point of the
        confirm step is that it does not.
        """
        url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ChecksumAlgorithm": "SHA256"},
            ExpiresIn=self.presign_secs,
        )
        return Upload(url=url, method="PUT", key=key, expires_in_secs=self.presign_secs)

    async def put(self, key: str, data: bytes) -> Stored:
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data,
            ChecksumAlgorithm="SHA256", ChecksumSHA256=digest,
        )
        return Stored(key=key, bytes=len(data), sha256=sha256_of(data))

    async def head(self, key: str) -> Stored | None:
        """Size and digest, both from the store rather than the uploader.

        S3 returns its checksum base64-encoded; the rest of the system
        speaks hex, because that is what the rig computes and what a
        person reads in a log.
        """
        try:
            info = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except Exception:
            return None

        digest = info.get("ChecksumSHA256")
        if digest:
            sha = base64.b64decode(digest).hex()
        else:
            # The object predates checksums, or was written by something
            # that did not ask for one. Reported as unknown rather than
            # guessed - confirm() refuses to permit a deletion it cannot
            # verify, which is the correct answer.
            sha = ""
        return Stored(key=key, bytes=int(info["ContentLength"]), sha256=sha)

    async def copy_to_archive(self, key: str) -> str:
        self.client.copy_object(
            Bucket=self.archive_bucket,
            Key=key,
            CopySource={"Bucket": self.bucket, "Key": key},
        )
        return f"{self.archive_bucket}/{key}"

    async def head_archive(self, key: str) -> Stored | None:
        """The same question as head(), asked of the archive bucket.

        Same rule as everywhere in this file: a checksum the store did not
        compute is not a checksum. An object with none comes back with an
        empty digest and the drain refuses to free the spool copy on the
        strength of it.
        """
        try:
            info = self.client.head_object(
                Bucket=self.archive_bucket, Key=key, ChecksumMode="ENABLED")
        except Exception:
            return None
        digest = info.get("ChecksumSHA256")
        sha = base64.b64decode(digest).hex() if digest else ""
        return Stored(key=key, bytes=int(info["ContentLength"]), sha256=sha)

    async def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    async def delete_archived(self, key: str) -> None:
        self.client.delete_object(Bucket=self.archive_bucket, Key=key)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        from core.infrastructure.config import get_settings

        s = get_settings()
        if s.storage_endpoint and s.storage_access_key:
            _storage = MinioStorage(
                endpoint=s.storage_endpoint,
                bucket=s.storage_bucket,
                access_key=s.storage_access_key,
                secret_key=s.storage_secret_key,
            )
        else:
            _storage = LocalStorage(s.storage_local_root)
    return _storage


def configure(storage: Storage) -> None:
    """Point everything at a different store. Tests call this."""
    global _storage
    _storage = storage
