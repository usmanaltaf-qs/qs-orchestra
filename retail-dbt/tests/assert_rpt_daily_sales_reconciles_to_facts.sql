-- Per day, rpt_daily_sales net_sales must match fct_order_lines and refunds must match fct_returns.
with rpt as (
    select date_day, sum(net_sales) as net_sales, sum(refunds) as refunds
    from {{ ref('rpt_daily_sales') }}
    group by date_day
),

lines as (
    select order_date as date_day, sum(net_sales) as net_sales
    from {{ ref('fct_order_lines') }}
    group by order_date
),

returns as (
    select return_date as date_day, sum(refund_amount) as refunds
    from {{ ref('fct_returns') }}
    group by return_date
)

select rpt.*, lines.net_sales as lines_net_sales, returns.refunds as returns_refunds
from rpt
left join lines on rpt.date_day = lines.date_day
left join returns on rpt.date_day = returns.date_day
where rpt.net_sales <> coalesce(lines.net_sales, 0)
   or rpt.refunds <> coalesce(returns.refunds, 0)
