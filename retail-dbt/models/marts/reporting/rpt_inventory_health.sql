-- Stock position by store and category on the latest snapshot.
with latest as (
    select max(snapshot_date) as snapshot_date
    from {{ ref('fct_inventory_snapshots') }}
),

snapshots as (
    select f.*, p.category, p.unit_cost
    from {{ ref('fct_inventory_snapshots') }} as f
    inner join latest as l on f.snapshot_date = l.snapshot_date
    inner join {{ ref('dim_products') }} as p on f.product_id = p.product_id
),

aggregated as (
    select
        snapshot_date,
        store_id,
        category,
        count(*) as products_tracked,
        sum(case when is_stockout then 1 else 0 end) as stockouts,
        sum(case when weeks_of_cover < 2 then 1 else 0 end) as low_cover_products,
        sum(on_hand_qty) as units_on_hand,
        sum(on_order_qty) as units_on_order,
        sum(trailing_28d_units_sold) as trailing_28d_units_sold,
        sum(on_hand_qty * unit_cost) as stock_value_at_cost
    from snapshots
    group by snapshot_date, store_id, category
)

select
    snapshot_date,
    store_id,
    category,
    products_tracked,
    stockouts,
    cast(1.0 * stockouts / products_tracked as decimal(8, 4)) as stockout_rate,
    low_cover_products,
    units_on_hand,
    units_on_order,
    trailing_28d_units_sold,
    cast(units_on_hand / nullif(trailing_28d_units_sold / 4.0, 0) as decimal(10, 2)) as weeks_of_cover,
    cast(stock_value_at_cost as decimal(12, 2)) as stock_value_at_cost
from aggregated
