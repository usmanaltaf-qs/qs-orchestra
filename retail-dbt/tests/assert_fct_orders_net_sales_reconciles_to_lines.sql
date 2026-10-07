-- fct_orders.net_sales must equal the sum of its lines in fct_order_lines.
select o.order_id, o.net_sales, l.net_sales as lines_net_sales
from {{ ref('fct_orders') }} as o
full outer join (
    select order_id, sum(net_sales) as net_sales
    from {{ ref('fct_order_lines') }}
    group by order_id
) as l on o.order_id = l.order_id
where o.net_sales is distinct from l.net_sales
