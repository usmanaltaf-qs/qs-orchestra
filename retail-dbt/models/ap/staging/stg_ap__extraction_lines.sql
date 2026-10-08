-- Lines of the latest extraction per file hash (same max(run_id) rule as stg_ap__extractions).
with source as (
    select * from {{ source('ap', 'extracted_invoice_lines') }}
),

latest as (
    select *
    from source
    qualify run_id = max(run_id) over (partition by file_hash)
)

select
    file_hash || '-' || cast(line_no as varchar) as extraction_line_id,
    file_hash,
    cast(line_no as integer) as invoice_line_no,
    description,
    {{ ap_norm_text('description') }} as description_norm,
    {{ ap_norm_id('sku') }} as sku,
    cast(quantity as decimal(12, 3)) as quantity,
    cast(unit_price as decimal(12, 2)) as unit_price,
    cast(vat_rate as decimal(5, 4)) as vat_rate,
    cast(line_net as decimal(12, 2)) as line_net
from latest
