with source as (
    select * from {{ source('ap', 'po_lines') }}
)

select
    {{ ap_norm_id('po_number') }} || '-' || cast(line_no as varchar) as po_line_id,
    {{ ap_norm_id('po_number') }} as po_number,
    cast(line_no as integer) as po_line_no,
    cast(product_id as integer) as product_id,
    {{ ap_norm_id('sku') }} as sku,
    description,
    {{ ap_norm_text('description') }} as description_norm,
    cast(qty_ordered as integer) as qty_ordered,
    cast(unit_cost as decimal(12, 2)) as unit_cost,
    cast(vat_rate as decimal(5, 4)) as vat_rate
from source
