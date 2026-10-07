select order_item_id, refund_amount, line_total
from {{ ref('fct_order_lines') }}
where refund_amount > line_total
