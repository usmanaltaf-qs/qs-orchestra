-- Order lines with order and product context plus the line-level sales measures.
-- Sales measures follow models.md: they count only completed orders, so they are 0 on
-- cancelled/pending lines (line_total keeps the raw line value).
with order_items as (
    select * from {{ ref('stg_retail__order_items') }}
),

orders as (
    select * from {{ ref('stg_retail__orders') }}
),

products as (
    select * from {{ ref('stg_retail__products') }}
),

joined as (
    select
        i.order_item_id,
        i.order_id,
        i.order_date,
        i.line_number,
        o.order_ts,
        o.status,
        o.channel,
        coalesce(o.store_id, 0) as store_id,  -- 0 = Online row in dim_stores
        o.customer_id,
        i.product_id,
        p.category,
        p.subcategory,
        p.brand,
        i.quantity,
        i.unit_price,
        i.discount_pct,
        i.line_total,
        p.unit_cost,
        o.status = 'completed' as is_completed
    from order_items as i
    inner join orders as o on i.order_id = o.order_id
    inner join products as p on i.product_id = p.product_id
)

select
    *,
    cast(case when is_completed then quantity * unit_price else 0 end as decimal(12, 2)) as gross_sales,
    cast(case when is_completed then line_total else 0 end as decimal(12, 2)) as net_sales,
    cast(case when is_completed then quantity * unit_price - line_total else 0 end as decimal(12, 2))
        as discount_amount,
    cast(case when is_completed then quantity * unit_cost else 0 end as decimal(12, 2)) as cogs
from joined
