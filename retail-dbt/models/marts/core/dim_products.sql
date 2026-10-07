with products as (
    select * from {{ ref('stg_retail__products') }}
)

select
    *,
    case
        when list_price < 10 then 'Under £10'
        when list_price < 25 then '£10-25'
        when list_price < 50 then '£25-50'
        when list_price < 100 then '£50-100'
        else '£100+'
    end as price_band,
    cast((list_price - unit_cost) / nullif(list_price, 0) as decimal(6, 4)) as margin_pct
from products
