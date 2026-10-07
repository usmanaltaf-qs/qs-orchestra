-- Customers with lifetime order stats. "As of" is the latest order date in the data rather
-- than today, so is_active and recency are stable for a given dataset.
with customers as (
    select * from {{ ref('stg_retail__customers') }}
),

customer_orders as (
    select * from {{ ref('int_customer_orders') }}
),

as_of as (
    select max(order_date) as as_of_date from {{ ref('stg_retail__orders') }}
),

ranked as (
    select
        customer_id,
        percent_rank() over (order by lifetime_net_sales desc) as sales_rank_pct
    from customer_orders
)

select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    c.city,
    c.region,
    c.signup_date,
    c.loyalty_tier,
    c.marketing_opt_in,
    o.first_order_date,
    cast({{ dbt.date_trunc('month', 'o.first_order_date') }} as date) as cohort_month,
    o.last_order_date,
    coalesce(o.order_count, 0) as lifetime_orders,
    cast(coalesce(o.lifetime_net_sales, 0) as decimal(12, 2)) as lifetime_net_sales,
    {{ dbt.datediff('o.last_order_date', 'a.as_of_date', 'day') }} as days_since_last_order,
    coalesce({{ dbt.datediff('o.last_order_date', 'a.as_of_date', 'day') }} < 90, false) as is_active,
    case
        when o.customer_id is null then 'No orders'
        when r.sales_rank_pct < 0.1 then 'Top 10%'
        when r.sales_rank_pct < 0.5 then 'Next 40%'
        else 'Rest'
    end as value_segment
from customers as c
cross join as_of as a
left join customer_orders as o on c.customer_id = o.customer_id
left join ranked as r on c.customer_id = r.customer_id
