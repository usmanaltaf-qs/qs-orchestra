-- Every return points at an order line, except returns for orders placed before the data
-- starts (the generator's 30-day lookback). order_item_id is date-keyed
-- ((yyyymmdd * 1e6 + seq) * 10 + line), so those have ids below the first loaded line.
select r.*
from {{ ref('stg_retail__returns') }} as r
left join {{ ref('stg_retail__order_items') }} as i
    on r.order_item_id = i.order_item_id
where i.order_item_id is null
  and r.order_item_id >= (select min(order_item_id) from {{ ref('stg_retail__order_items') }})
