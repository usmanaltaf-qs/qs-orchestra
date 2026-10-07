with source as (
    select * from {{ source('retail', 'returns') }}
)

select
    cast(return_id as bigint) as return_id,
    cast(order_item_id as bigint) as order_item_id,
    cast(return_date as date) as return_date,
    reason,
    cast(refund_amount as decimal(12, 2)) as refund_amount
from source
