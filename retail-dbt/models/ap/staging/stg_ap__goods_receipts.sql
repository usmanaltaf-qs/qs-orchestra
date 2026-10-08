with source as (
    select * from {{ source('ap', 'goods_receipts') }}
)

select
    {{ ap_norm_id('grn_number') }} as grn_number,
    {{ ap_norm_id('po_number') }} as po_number,
    cast(received_date as date) as received_date
from source
