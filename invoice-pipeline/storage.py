"""Files on local disk or in GCS, behind one small API. Paths are plain strings: either a local
path or gs://bucket/key. (pathlib collapses gs:// to gs:/, so never wrap a remote path in Path.)

GCS access uses the HMAC keys (GCS_HMAC_KEY_ID / GCS_HMAC_SECRET) for both halves:
- objects (PDFs, JSON): boto3 against GCS's S3-interoperable XML API
- Parquet: DuckDB httpfs with a GCS secret (see duck())
so Orchestra needs one credential type, the same one the retail pipeline uses.
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

import duckdb

GCS_ENDPOINT = "https://storage.googleapis.com"


def is_gcs(path: str) -> bool:
    return str(path).startswith("gs://")


def join(*parts: str) -> str:
    head, *rest = [str(p) for p in parts]
    out = head.rstrip("/")
    for p in rest:
        out += "/" + p.strip("/")
    return out


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("gs://").partition("/")
    return bucket, key


def _hmac() -> tuple[str, str]:
    key_id, secret = os.environ.get("GCS_HMAC_KEY_ID"), os.environ.get("GCS_HMAC_SECRET")
    if not key_id or not secret:
        raise SystemExit("gs:// paths need GCS_HMAC_KEY_ID and GCS_HMAC_SECRET "
                         "(HMAC keys from Cloud Storage > Settings > Interoperability)")
    return key_id, secret


@lru_cache(maxsize=1)
def _s3():
    import boto3
    from botocore.config import Config

    key_id, secret = _hmac()
    return boto3.client(
        "s3", endpoint_url=GCS_ENDPOINT, aws_access_key_id=key_id, aws_secret_access_key=secret,
        region_name="auto",
        # GCS rejects the default CRC checksum headers newer boto3 sends
        config=Config(signature_version="s3v4", request_checksum_calculation="when_required",
                      response_checksum_validation="when_required", retries={"max_attempts": 5}),
    )


def duck(path_hint: str | None = None, con: duckdb.DuckDBPyConnection | None = None):
    """A DuckDB connection that can read and write gs:// when path_hint is remote."""
    con = con or duckdb.connect()
    if path_hint and is_gcs(path_hint):
        key_id, secret = _hmac()
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731  (CREATE SECRET takes no parameters)
        con.execute(f"CREATE OR REPLACE SECRET gcs (TYPE gcs, KEY_ID {q(key_id)}, SECRET {q(secret)})")
    return con


def read_bytes(path: str) -> bytes:
    if is_gcs(path):
        bucket, key = _split(path)
        return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    return Path(path).read_bytes()


def write_bytes(path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    if is_gcs(path):
        bucket, key = _split(path)
        _s3().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)


def list_files(prefix: str, suffix: str = "") -> list[str]:
    """Every file under prefix (recursively) ending in suffix, as full paths, sorted."""
    if is_gcs(prefix):
        bucket, key = _split(prefix)
        key = key.rstrip("/") + "/"
        out = []
        for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key):
            out += [f"gs://{bucket}/{o['Key']}" for o in page.get("Contents", []) if o["Key"].endswith(suffix)]
        return sorted(out)
    root = Path(prefix)
    if not root.exists():
        return []
    # build from the prefix string as given (Path would drop a leading "./", breaking relative())
    return sorted(join(prefix, p.relative_to(root).as_posix()) for p in root.rglob(f"*{suffix}") if p.is_file())


def exists(path: str) -> bool:
    if is_gcs(path):
        bucket, key = _split(path)
        try:
            _s3().head_object(Bucket=bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001  (404 and friends)
            return False
    return Path(path).exists()


def delete_prefix(prefix: str) -> int:
    """Delete everything under prefix. One delete per object: GCS's S3 API has no batch delete."""
    if is_gcs(prefix):
        files = list_files(prefix)
        for f in files:
            bucket, key = _split(f)
            _s3().delete_object(Bucket=bucket, Key=key)
        return len(files)
    if Path(prefix).exists():
        shutil.rmtree(prefix)
    return 0


def relative(path: str, root: str) -> str:
    return str(path)[len(str(root).rstrip("/")) + 1:]
