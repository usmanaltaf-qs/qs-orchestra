-- Retention triangle: customers by month of first completed order, and how many of them
-- ordered again N months later. The earliest cohort also absorbs existing customers whose
-- real first order predates the data.
with customers as (
    select customer_id, cohort_month
    from {{ ref('dim_customers') }}
    where cohort_month is not null
),

orders as (
    select customer_id, order_date, net_sales
    from {{ ref('fct_orders') }}
    where status = 'completed' and customer_id is not null
),

cohort_sizes as (
    select cohort_month, count(*) as cohort_size
    from customers
    group by cohort_month
),

activity as (
    select
        c.cohort_month,
        {{ dbt.datediff('c.cohort_month', dbt.date_trunc('month', 'o.order_date'), 'month') }}
            as months_since_first_order,
        count(distinct o.customer_id) as active_customers,
        count(*) as orders,
        sum(o.net_sales) as net_sales
    from orders as o
    inner join customers as c on o.customer_id = c.customer_id
    group by 1, 2
)

select
    a.cohort_month,
    a.months_since_first_order,
    s.cohort_size,
    a.active_customers,
    cast(1.0 * a.active_customers / s.cohort_size as decimal(8, 4)) as retention_rate,
    a.orders,
    cast(a.net_sales as decimal(12, 2)) as net_sales
from activity as a
inner join cohort_sizes as s on a.cohort_month = s.cohort_month
