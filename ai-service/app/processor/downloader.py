from typing import Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from ..core.config import Settings


class ObjectNotFoundError(Exception):
    """The storage key does not exist (or the bucket is unreachable)."""


class ObjectDownloader(Protocol):
    def get(self, key: str) -> bytes: ...

    def reachable(self) -> bool: ...


class S3Downloader:
    """Downloads objects from the Day-8 S3-compatible store (MinIO).

    boto3 mirrors the backend's @aws-sdk/client-s3 usage: the same endpoint,
    path-style addressing for MinIO, and the same dev credentials. The client
    is created lazily on first use and rebound on failure so a dead MinIO
    doesn't leave a poisoned client object behind.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        self._client = boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint,
            region_name=self._settings.s3_region,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 2},
                s3={"addressing_style": "path" if self._settings.s3_force_path_style else "auto"},
            ),
        )
        return self._client

    def get(self, key: str) -> bytes:
        try:
            response = self._ensure_client().get_object(
                Bucket=self._settings.s3_bucket, Key=key
            )
            return response["Body"].read()
        except ClientError as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "NotFound", "NoSuchBucket"}:
                raise ObjectNotFoundError(f"{self._settings.s3_bucket}/{key}: {code}") from exc
            raise
        finally:
            # a stream that wasn't fully read is cheap to drop; S3 body objects
            # close cleanly and keep the pooled connection reusable
            pass

    def reachable(self) -> bool:
        try:
            self._client = None  # force a fresh client for the probe
            self._ensure_client().head_bucket(Bucket=self._settings.s3_bucket)
            return True
        except Exception:
            return False

    def put(self, key: str, content: bytes) -> None:
        """Used by tests to plant objects; harmless in production."""
        self._ensure_client().put_object(
            Bucket=self._settings.s3_bucket, Key=key, Body=content
        )

    def delete(self, key: str) -> None:
        self._ensure_client().delete_object(
            Bucket=self._settings.s3_bucket, Key=key
        )