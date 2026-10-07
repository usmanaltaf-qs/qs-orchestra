-- Total net sales and completed orders in rpt_kpi_daily must match fct_orders.
select k.net_sales, k.completed_orders, o.net_sales as fct_net_sales, o.completed_orders as fct_completed_orders
from (select sum(net_sales) as net_sales, sum(completed_orders) as completed_orders
      from {{ ref('rpt_kpi_daily') }}) as k
cross join (select sum(net_sales) as net_sales, sum(case when status = 'completed' then 1 else 0 end) as completed_orders
            from {{ ref('fct_orders') }}) as o
where k.net_sales <> o.net_sales or k.completed_orders <> o.completed_orders
