with source as (
    select * from {{ source('retail', 'inventory_snapshots') }}
)

select
    cast(snapshot_date as date) as snapshot_date,
    cast(store_id as integer) as store_id,
    cast(product_id as integer) as product_id,
    cast(on_hand_qty as integer) as on_hand_qty,
    cast(on_order_qty as integer) as on_order_qty
from source
