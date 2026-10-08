-- Exception explanations from explain_exceptions.py: latest per (invoice, check_hash).
-- Read with ap_optional_parquet rather than a source: there are none until the first explain
-- run, which comes after the first dbt run.
{% set root = env_var('INVOICES_EXTRACTED_URI', env_var('INVOICES_SOURCE_URI', '../data/invoices/dev')) %}
with source as (
    {{ ap_optional_parquet(root ~ '/explanations/*.parquet',
        'invoice_id VARCHAR, check_hash VARCHAR, run_id VARCHAR, created_at TIMESTAMPTZ, summary VARCHAR,
         suggested_action VARCHAR, details VARCHAR, source VARCHAR, fallback_reason VARCHAR, model VARCHAR,
         prompt_version VARCHAR, input_tokens INTEGER, output_tokens INTEGER') }}
)

select
    invoice_id || '-' || check_hash as explanation_id,
    invoice_id,
    check_hash,
    created_at,
    summary,
    suggested_action,
    details,
    source as explanation_source,
    fallback_reason,
    model,
    prompt_version,
    input_tokens,
    output_tokens
from source
qualify row_number() over (partition by invoice_id, check_hash order by created_at desc, run_id desc) = 1
