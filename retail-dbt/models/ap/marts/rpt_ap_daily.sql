-- Daily counts and values by final status and exception type. An invoice with several
-- exception types appears once per type, so sum invoices within a type, not across types.
with invoices as (
    select * from {{ ref('fct_invoice_status') }}
),

by_type as (
    select
        i.received_date,
        i.final_status,
        coalesce(c.check_name, 'none') as exception_type,
        i.invoice_id,
        i.total_gross,
        i.value_on_hold
    from invoices as i
    left join {{ ref('int_ap__invoice_checks') }} as c
        on i.invoice_id = c.invoice_id and c.passed = false
)

select
    received_date,
    final_status,
    exception_type,
    count(distinct invoice_id) as invoices,
    cast(sum(total_gross) as decimal(12, 2)) as total_gross,
    cast(sum(value_on_hold) as decimal(12, 2)) as value_on_hold
from by_type
group by received_date, final_status, exception_type
