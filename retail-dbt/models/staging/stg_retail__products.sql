with source as (
    select * from {{ source('retail', 'products') }}
)

select
    cast(product_id as integer) as product_id,
    sku,
    product_name,
    category,
    subcategory,
    brand,
    cast(list_price as decimal(12, 2)) as list_price,
    cast(unit_cost as decimal(12, 2)) as unit_cost,
    is_active
from source
