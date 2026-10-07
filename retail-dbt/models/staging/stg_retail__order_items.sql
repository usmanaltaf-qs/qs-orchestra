with source as (
    select * from {{ source('retail', 'order_items') }}
)

select
    cast(order_item_id as bigint) as order_item_id,
    cast(order_id as bigint) as order_id,
    cast(order_date as date) as order_date,
    cast(line_number as integer) as line_number,
    cast(product_id as integer) as product_id,
    cast(quantity as integer) as quantity,
    cast(unit_price as decimal(12, 2)) as unit_price,
    cast(discount_pct as decimal(4, 2)) as discount_pct,
    cast(line_total as decimal(12, 2)) as line_total
from source
