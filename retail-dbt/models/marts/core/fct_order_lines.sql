-- Incremental on order_item_id. Each run reprocesses the last N days of orders, plus any
-- older line returned in the last N days: returns land up to 30 days after the order, and
-- an order_date-only lookback would leave is_returned/refund_amount stale on those lines.
{{ config(
    materialized='incremental',
    unique_key='order_item_id',
    incremental_strategy='merge',
    on_schema_change='append_new_columns'
) }}

{% set lookback = var('incremental_lookback_days') %}

with lines as (
    select * from {{ ref('int_order_lines_enriched') }}
    {% if is_incremental() %}
    where order_date >= (select cast({{ dbt.dateadd('day', -lookback, 'max(order_date)') }} as date) from {{ this }})
       or order_item_id in (
            select order_item_id from {{ ref('stg_retail__returns') }}
            where return_date >= (select cast({{ dbt.dateadd('day', -lookback, 'max(order_date)') }} as date) from {{ this }})
       )
    {% endif %}
),

returns as (
    select order_item_id, return_date, refund_amount
    from {{ ref('stg_retail__returns') }}
)

select
    l.*,
    r.order_item_id is not null as is_returned,
    r.return_date,
    cast(coalesce(r.refund_amount, 0) as decimal(12, 2)) as refund_amount
from lines as l
left join returns as r on l.order_item_id = r.order_item_id
