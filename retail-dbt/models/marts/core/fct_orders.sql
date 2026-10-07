-- One row per order, all statuses. Sales measures are 0 for cancelled/pending orders;
-- order_value keeps the raw basket value so cancellations can still be sized.
with lines as (
    select * from {{ ref('fct_order_lines') }}
)

select
    order_id,
    order_date,
    order_ts,
    status,
    channel,
    store_id,
    customer_id,
    customer_id is null as is_guest,
    count(*) as item_count,
    sum(quantity) as units,
    cast(sum(line_total) as decimal(12, 2)) as order_value,
    cast(sum(gross_sales) as decimal(12, 2)) as gross_sales,
    cast(sum(discount_amount) as decimal(12, 2)) as discount_amount,
    cast(sum(net_sales) as decimal(12, 2)) as net_sales,
    cast(sum(cogs) as decimal(12, 2)) as cogs,
    max(case when is_returned then 1 else 0 end) = 1 as has_return,
    cast(sum(refund_amount) as decimal(12, 2)) as refund_amount
from lines
group by order_id, order_date, order_ts, status, channel, store_id, customer_id
