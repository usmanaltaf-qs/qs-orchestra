select r.return_id, o.order_id, o.status
from {{ ref('fct_returns') }} as r
inner join {{ ref('fct_orders') }} as o on r.order_id = o.order_id
where o.status = 'cancelled'
