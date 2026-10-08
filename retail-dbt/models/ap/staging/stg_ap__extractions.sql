-- Latest extraction per distinct PDF content (file hash). Byte-identical files share one
-- extraction; int_ap__invoices fans it out to every inbox file.
-- run_id is a UTC timestamp, so max(run_id) is the latest run (same rule as the lines model).
with source as (
    select * from {{ source('ap', 'extracted_invoices') }}
),

latest as (
    select *
    from source
    qualify run_id = max(run_id) over (partition by file_hash)
)

select
    file_hash,
    run_id,
    extracted_at,
    model,
    prompt_version,
    input_tokens,
    output_tokens,
    status as extraction_status,
    error as extraction_error,
    supplier_name,
    {{ ap_norm_name('supplier_name') }} as supplier_name_norm,
    {{ ap_norm_id('supplier_vat_number') }} as supplier_vat_number,
    {{ ap_norm_id('invoice_number') }} as invoice_number,
    cast(invoice_date as date) as invoice_date,
    {{ ap_norm_id('po_number') }} as po_number,
    upper(trim(currency)) as currency,
    cast(subtotal_net as decimal(12, 2)) as subtotal_net,
    cast(vat_total as decimal(12, 2)) as vat_total,
    cast(total_gross as decimal(12, 2)) as total_gross,
    {{ ap_digits('bank_sort_code') }} as bank_sort_code,
    {{ ap_digits('bank_account') }} as bank_account,
    coalesce(is_copy_or_duplicate_marked, false) as is_copy_or_duplicate_marked,
    extraction_notes,
    cast(line_count as integer) as line_count
from latest
