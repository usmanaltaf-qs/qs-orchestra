-- One row per return. Returns in the first 30 days of data can belong to orders placed
-- before the data starts; those keep has_order_line = false and NULL order attributes.
with returns as (
    select * from {{ ref('stg_retail__returns') }}
),

lines as (
    select * from {{ ref('fct_order_lines') }}
)

select
    r.return_id,
    r.order_item_id,
    l.order_id,
    l.order_date,
    r.return_date,
    {{ dbt.datediff('l.order_date', 'r.return_date', 'day') }} as days_to_return,
    r.reason,
    r.refund_amount,
    l.line_total,
    l.product_id,
    l.category,
    l.channel,
    l.store_id,
    l.customer_id,
    l.order_item_id is not null as has_order_line
from returns as r
left join lines as l on r.order_item_id = l.order_item_id
