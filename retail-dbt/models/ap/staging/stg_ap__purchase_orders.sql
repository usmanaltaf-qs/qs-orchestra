with source as (
    select * from {{ source('ap', 'purchase_orders') }}
)

select
    {{ ap_norm_id('po_number') }} as po_number,
    supplier_id,
    cast(store_id as integer) as store_id,
    cast(order_date as date) as order_date,
    currency
from source
