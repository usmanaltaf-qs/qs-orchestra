-- Exec headline KPIs per day over the data range, with week-on-week (vs 7 days earlier) and
-- year-on-year (vs 364 days earlier, same weekday) changes. Definitions: models.md.
with bounds as (
    select min(order_date) as first_day, max(order_date) as last_day
    from {{ ref('fct_orders') }}
),

days as (
    select d.date_day
    from {{ ref('dim_date') }} as d
    inner join bounds as b on d.date_day between b.first_day and b.last_day
),

orders_daily as (
    select
        order_date as date_day,
        count(*) as total_orders,
        sum(case when status = 'completed' then 1 else 0 end) as completed_orders,
        sum(case when status = 'cancelled' then 1 else 0 end) as cancelled_orders,
        sum(gross_sales) as gross_sales,
        sum(discount_amount) as discount_amount,
        sum(net_sales) as net_sales,
        sum(cogs) as cogs
    from {{ ref('fct_orders') }}
    group by order_date
),

refunds_daily as (
    select return_date as date_day, sum(refund_amount) as refunds
    from {{ ref('fct_returns') }}
    group by return_date
),

daily as (
    select
        d.date_day,
        coalesce(o.total_orders, 0) as total_orders,
        coalesce(o.completed_orders, 0) as completed_orders,
        coalesce(o.cancelled_orders, 0) as cancelled_orders,
        cast(coalesce(o.gross_sales, 0) as decimal(12, 2)) as gross_sales,
        cast(coalesce(o.discount_amount, 0) as decimal(12, 2)) as discount_amount,
        cast(coalesce(o.net_sales, 0) as decimal(12, 2)) as net_sales,
        cast(coalesce(o.cogs, 0) as decimal(12, 2)) as cogs,
        cast(coalesce(r.refunds, 0) as decimal(12, 2)) as refunds
    from days as d
    left join orders_daily as o on d.date_day = o.date_day
    left join refunds_daily as r on d.date_day = r.date_day
),

kpis as (
    select
        *,
        cast(net_sales - cogs as decimal(12, 2)) as gross_margin,
        cast((net_sales - cogs) / nullif(net_sales, 0) as decimal(8, 4)) as gross_margin_pct,
        cast(net_sales - refunds as decimal(12, 2)) as net_sales_after_returns,
        cast(net_sales / nullif(completed_orders, 0) as decimal(12, 2)) as aov,
        cast(refunds / nullif(net_sales, 0) as decimal(8, 4)) as return_rate,
        cast(1.0 * cancelled_orders / nullif(total_orders, 0) as decimal(8, 4)) as cancellation_rate
    from daily
)

select
    k.*,
    cast((k.net_sales - w.net_sales) / nullif(w.net_sales, 0) as decimal(10, 4)) as net_sales_wow_pct,
    cast(1.0 * (k.completed_orders - w.completed_orders) / nullif(w.completed_orders, 0) as decimal(10, 4))
        as completed_orders_wow_pct,
    cast((k.aov - w.aov) / nullif(w.aov, 0) as decimal(10, 4)) as aov_wow_pct,
    cast((k.net_sales - y.net_sales) / nullif(y.net_sales, 0) as decimal(10, 4)) as net_sales_yoy_pct,
    cast(1.0 * (k.completed_orders - y.completed_orders) / nullif(y.completed_orders, 0) as decimal(10, 4))
        as completed_orders_yoy_pct,
    cast((k.aov - y.aov) / nullif(y.aov, 0) as decimal(10, 4)) as aov_yoy_pct
from kpis as k
left join kpis as w on w.date_day = cast({{ dbt.dateadd('day', -7, 'k.date_day') }} as date)
left join kpis as y on y.date_day = cast({{ dbt.dateadd('day', -364, 'k.date_day') }} as date)
