"""Local behaviour of the storage layer (GCS uses the same functions over boto3/DuckDB)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import storage  # noqa: E402


def test_paths_round_trip_with_dot_prefixed_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    storage.write_bytes("./env/inbox/2026/10/05/a.pdf", b"x")
    files = storage.list_files("./env/inbox", ".pdf")
    assert files == ["./env/inbox/2026/10/05/a.pdf"]
    assert storage.relative(files[0], "./env") == "inbox/2026/10/05/a.pdf"
    assert storage.read_bytes(files[0]) == b"x" and storage.exists(files[0])


def test_join_and_gcs_detection():
    assert storage.join("gs://b/dev/", "/invoices", "dev") == "gs://b/dev/invoices/dev"
    assert storage.is_gcs("gs://b/x") and not storage.is_gcs("./data")
    assert storage.relative("gs://b/root/inbox/a.pdf", "gs://b/root") == "inbox/a.pdf"
