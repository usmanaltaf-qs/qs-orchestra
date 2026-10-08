-- Match each invoice's printed supplier to master data: VAT number first, then normalised name.
with invoices as (
    select invoice_id, supplier_vat_number, supplier_name_norm from {{ ref('int_ap__invoices') }}
),

suppliers as (
    select supplier_id, vat_number, supplier_name_norm from {{ ref('stg_ap__suppliers') }}
)

select
    i.invoice_id,
    coalesce(by_vat.supplier_id, by_name.supplier_id) as supplier_id,
    case
        when by_vat.supplier_id is not null then 'vat_number'
        when by_name.supplier_id is not null then 'name'
    end as supplier_match_method
from invoices as i
left join suppliers as by_vat on i.supplier_vat_number = by_vat.vat_number
left join suppliers as by_name on i.supplier_name_norm = by_name.supplier_name_norm
