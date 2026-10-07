-- Stock snapshots with stockout flag and cover. Units sold are completed in-store sales of
-- that product at that store in the 28 days ending on the snapshot date.
with snapshots as (
    select * from {{ ref('stg_retail__inventory_snapshots') }}
),

daily_units as (
    select store_id, product_id, order_date, sum(quantity) as units
    from {{ ref('fct_order_lines') }}
    where is_completed and store_id <> 0
    group by store_id, product_id, order_date
),

with_sales as (
    select
        s.snapshot_date,
        s.store_id,
        s.product_id,
        s.on_hand_qty,
        s.on_order_qty,
        coalesce(sum(u.units), 0) as trailing_28d_units_sold
    from snapshots as s
    left join daily_units as u
        on s.store_id = u.store_id
        and s.product_id = u.product_id
        and u.order_date > cast({{ dbt.dateadd('day', -28, 's.snapshot_date') }} as date)
        and u.order_date <= s.snapshot_date
    group by s.snapshot_date, s.store_id, s.product_id, s.on_hand_qty, s.on_order_qty
)

select
    *,
    on_hand_qty = 0 as is_stockout,
    cast(on_hand_qty / nullif(trailing_28d_units_sold / 4.0, 0) as decimal(10, 2)) as weeks_of_cover
from with_sales
