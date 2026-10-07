# Storing the data in GCS

## Pick the right option

| | Parquet in GCS (default) | `.duckdb` file in GCS |
|---|---|---|
| How | DuckDB writes `gs://` directly via httpfs | build locally, upload with `google-cloud-storage` |
| Incremental runs | write one date partition | download → modify → re-upload whole file |
| Readable by | DuckDB, BigQuery external tables, Spark, dbt-duckdb, Polars | DuckDB only |
| Concurrent writers | fine (separate partitions) | no — last upload wins |

DuckDB can **read** a database file over GCS (`ATTACH 'gs://…/retail.duckdb' (READ_ONLY)`)
but can't write to it in place. So: Parquet for the pipeline, `.duckdb` upload only as a
convenience snapshot for ad-hoc querying.

## Auth

### DuckDB → GCS (Parquet target)
DuckDB's httpfs talks to GCS through its S3-compatible API, which needs **HMAC keys**
(Cloud Storage → Settings → Interoperability, tied to a service account).

```python
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute(f"""
  CREATE OR REPLACE SECRET gcs (
    TYPE gcs,
    KEY_ID '{os.environ["GCS_HMAC_KEY_ID"]}',
    SECRET '{os.environ["GCS_HMAC_SECRET"]}'
  )
""")
```
Only create the secret when `--output` starts with `gs://`. Fail fast with a clear message if
the env vars are missing. Never log the secret values.

Check the current DuckDB docs when building — newer versions may support credential-chain
auth for GCS, which would remove the HMAC step.

### Python → GCS (`--upload-duckdb`)
Use `google-cloud-storage` with Application Default Credentials. For environments without
ADC (Orchestra), support `GCP_SERVICE_ACCOUNT_JSON` containing the key JSON: write it to a
temp file and point `GOOGLE_APPLICATION_CREDENTIALS` at it before creating the client.
Make `google-cloud-storage` an optional import — only needed when this flag is used.

## Service account permissions
- Writer (generator): `roles/storage.objectAdmin` on the bucket (needs delete for overwrites)
- Reader (dbt/validation): `roles/storage.objectViewer`

## Bucket setup (one-off, user runs this)
```bash
gcloud storage buckets create gs://<bucket> --location=europe-west2 --uniform-bucket-level-access
gcloud iam service-accounts create retail-generator
gcloud storage buckets add-iam-policy-binding gs://<bucket> \
  --member=serviceAccount:retail-generator@<project>.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin
# then create HMAC keys for that SA in the console (Interoperability tab)
```
Optional: a lifecycle rule deleting objects under `dev/` after 30 days.

## Path convention
`gs://<bucket>/retail/<env>/` where env = dev | prod, so Orchestra environments can point
at different prefixes via one env var.
