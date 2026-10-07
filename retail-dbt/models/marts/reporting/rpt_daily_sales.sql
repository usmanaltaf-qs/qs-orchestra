-- Date x store x category x channel. Sales are by order_date, refunds by return_date (per
-- models.md), full-outer-joined on the reporting date. Refunds for orders placed before the
-- data starts have no order line, so their store/category/channel are NULL.
-- No order counts here: orders span categories, so they don't sum across this grain.
with sales as (
    select
        order_date as date_day,
        store_id,
        category,
        channel,
        sum(case when is_completed then 1 else 0 end) as order_lines,
        sum(case when is_completed then quantity else 0 end) as units,
        sum(gross_sales) as gross_sales,
        sum(discount_amount) as discount_amount,
        sum(net_sales) as net_sales,
        sum(cogs) as cogs
    from {{ ref('fct_order_lines') }}
    group by order_date, store_id, category, channel
),

refunds as (
    select
        return_date as date_day,
        store_id,
        category,
        channel,
        count(*) as returns,
        sum(refund_amount) as refunds
    from {{ ref('fct_returns') }}
    group by return_date, store_id, category, channel
),

joined as (
    select
        coalesce(s.date_day, r.date_day) as date_day,
        coalesce(s.store_id, r.store_id) as store_id,
        coalesce(s.category, r.category) as category,
        coalesce(s.channel, r.channel) as channel,
        coalesce(s.order_lines, 0) as order_lines,
        coalesce(s.units, 0) as units,
        coalesce(s.gross_sales, 0) as gross_sales,
        coalesce(s.discount_amount, 0) as discount_amount,
        coalesce(s.net_sales, 0) as net_sales,
        coalesce(s.cogs, 0) as cogs,
        coalesce(r.returns, 0) as returns,
        coalesce(r.refunds, 0) as refunds
    from sales as s
    full outer join refunds as r
        on s.date_day = r.date_day
        and s.store_id = r.store_id
        and s.category = r.category
        and s.channel = r.channel
)

select
    date_day,
    store_id,
    category,
    channel,
    order_lines,
    units,
    returns,
    cast(gross_sales as decimal(12, 2)) as gross_sales,
    cast(discount_amount as decimal(12, 2)) as discount_amount,
    cast(net_sales as decimal(12, 2)) as net_sales,
    cast(cogs as decimal(12, 2)) as cogs,
    cast(net_sales - cogs as decimal(12, 2)) as gross_margin,
    cast(refunds as decimal(12, 2)) as refunds,
    cast(net_sales - refunds as decimal(12, 2)) as net_sales_after_returns
from joined
