"""File upload helper backed by S3-compatible object storage.

Configured for Linode Object Storage, which speaks S3 with a regional
endpoint::

    S3_ENDPOINT=https://eu-central-1.linodeobjects.com
    S3_REGION=eu-central-1
    S3_BUCKET=autoplan
    LINODE_ACCESS_KEY_ID=...
    LINODE_SECRET_ACCESS_KEY=...

Any S3 service works -- AWS included -- by pointing S3_ENDPOINT at it, or
leaving it unset for AWS proper. The keys are read from the LINODE_* names
first and fall back to the standard AWS_* ones, so a deployment already
carrying AWS credentials needs no new names.

Replaces Cloudinary, whose plan capped a single upload at 10 MB -- a size a
survey plan's DWG passes easily.
"""

import logging
import mimetypes
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Uploaded artefacts are downloaded by whoever the plan is shared with, so they
# are readable without a signature. Objects are keyed by plan and timestamp,
# which is not a secret worth protecting but is not guessable either.
DEFAULT_ACL = "public-read"

_client = None
_client_lock = threading.Lock()


def _setting(*names: str) -> Optional[str]:
    """First of these environment variables that is set and non-empty."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


def bucket_name() -> Optional[str]:
    return _setting("S3_BUCKET", "AWS_BUCKET")


def is_configured() -> bool:
    """Whether there is enough configuration to upload anything."""
    return bool(
        bucket_name()
        and _setting("LINODE_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY")
        and _setting("LINODE_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY")
    )


def _get_client():
    """Build the S3 client once and reuse it.

    Lazily, and behind a lock: the client is built from environment variables
    that are not read until something is actually uploaded, so importing this
    module never fails on a machine that has no storage configured.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client

        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=_setting("S3_ENDPOINT"),
            region_name=_setting("S3_REGION", "AWS_REGION") or "us-east-1",
            aws_access_key_id=_setting(
                "LINODE_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY"),
            aws_secret_access_key=_setting(
                "LINODE_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY"),
            # Retries matter here: this runs at the end of a plan that may have
            # taken minutes to draw, and losing it to one flaky request would
            # mean drawing the whole thing again.
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return _client


def public_url(key: str) -> str:
    """Permanent URL for an object, derived from the endpoint.

    Path style rather than virtual-hosted -- ``endpoint/bucket/key`` -- because
    it works whether or not the bucket name resolves as a subdomain, which for
    a custom endpoint is not something to assume.
    """
    bucket = bucket_name()
    endpoint = _setting("S3_ENDPOINT")
    if not endpoint:
        region = _setting("S3_REGION", "AWS_REGION") or "us-east-1"
        return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
    return f"{endpoint.rstrip('/')}/{bucket}/{key}"


def upload_file(file_path: str, folder: str = "uploads", file_name: str = None):
    """Upload a file and return its public URL, or ``None`` if it failed.

    Returning ``None`` rather than raising keeps the drawing itself the thing
    that decides whether a plan succeeded: the sheet is already on disk by the
    time this runs, and a storage outage should not read as a failed plan.
    """
    if not is_configured():
        logger.error(
            "Object storage is not configured: set S3_BUCKET, S3_ENDPOINT and "
            "the LINODE_ACCESS_KEY_ID / LINODE_SECRET_ACCESS_KEY pair"
        )
        return None

    name = file_name or os.path.basename(file_path)
    key = f"{folder.strip('/')}/{name}" if folder else name
    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    try:
        # upload_file, not put_object: it switches to a multipart upload above
        # a threshold on its own, so a large DWG does not have to fit in memory
        # and is not capped by a single request.
        _get_client().upload_file(
            file_path,
            bucket_name(),
            key,
            ExtraArgs={"ContentType": content_type, "ACL": DEFAULT_ACL},
        )
        return public_url(key)
    except Exception:
        logger.exception("Upload to object storage failed (%s)", key)
        return None


def delete_file(key: str) -> bool:
    """Remove an object. Best effort -- a leftover file is not worth an error."""
    if not is_configured():
        return False
    try:
        _get_client().delete_object(Bucket=bucket_name(), Key=key)
        return True
    except Exception:
        logger.warning("Could not delete %s", key, exc_info=True)
        return False
