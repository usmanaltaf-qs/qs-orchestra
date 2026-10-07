with source as (
    select * from {{ source('retail', 'orders') }}
)

select
    cast(order_id as bigint) as order_id,
    cast(order_date as date) as order_date,
    cast(order_ts as timestamp) as order_ts,
    channel,
    cast(store_id as integer) as store_id,
    cast(customer_id as integer) as customer_id,
    status
from source
