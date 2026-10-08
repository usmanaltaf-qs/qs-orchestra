with source as (
    select * from {{ source('ap', 'grn_lines') }}
)

select
    {{ ap_norm_id('grn_number') }} || '-' || cast(line_no as varchar) as grn_line_id,
    {{ ap_norm_id('grn_number') }} as grn_number,
    {{ ap_norm_id('po_number') }} as po_number,
    cast(line_no as integer) as po_line_no,
    cast(product_id as integer) as product_id,
    cast(qty_received as integer) as qty_received
from source
