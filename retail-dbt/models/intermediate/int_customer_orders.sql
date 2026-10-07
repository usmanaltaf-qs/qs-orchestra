-- Per-customer order history from completed orders (guests excluded).
with lines as (
    select * from {{ ref('int_order_lines_enriched') }}
    where is_completed and customer_id is not null
)

select
    customer_id,
    min(order_date) as first_order_date,
    max(order_date) as last_order_date,
    count(distinct order_id) as order_count,
    cast(sum(net_sales) as decimal(12, 2)) as lifetime_net_sales
from lines
group by customer_id
