-- One row per invoice. Status comes only from the rule checks (and, once the review loop
-- exists, human decisions). Precedence: duplicate > needs_review > exception > auto_approved.
with invoices as (
    select * from {{ ref('int_ap__invoices') }}
),

checks as (
    select * from {{ ref('int_ap__invoice_checks') }}
),

failed as (
    select
        invoice_id,
        count(*) filter (where passed = false) as failed_check_count,
        bool_or(check_name = 'duplicate' and passed = false) as is_duplicate,
        string_agg(check_name, ',' order by check_name) filter (where passed = false) as exception_types,
        -- changes whenever the failed checks or their values change: explanations key on it
        md5(coalesce(string_agg(check_name || '|' || coalesce(expected, '') || '|' || coalesce(actual, ''), '#'
                                order by check_name) filter (where passed = false), '')) as check_hash
    from checks
    group by invoice_id
),

status as (
    select
        i.invoice_id,
        case
            when f.is_duplicate then 'duplicate'
            when i.extraction_status <> 'ok' then 'needs_review'
            when f.failed_check_count > 0 then 'exception'
            else 'auto_approved'
        end as status
    from invoices as i
    inner join failed as f on i.invoice_id = f.invoice_id
)

select
    i.invoice_id,
    i.file_path,
    i.file_hash,
    i.received_date,
    i.invoice_date,
    s.supplier_id,
    coalesce(m.supplier_name, i.supplier_name) as supplier_name,
    i.invoice_number,
    i.po_number,
    i.subtotal_net,
    i.vat_total,
    i.total_gross,
    i.extraction_status,
    i.model,
    i.prompt_version,
    i.input_tokens,
    i.output_tokens,
    st.status,
    f.exception_types,
    f.failed_check_count,
    f.check_hash,
    -- review decisions land in step 5; until then the final status is the system status
    st.status as final_status,
    case when st.status in ('exception', 'needs_review') then i.total_gross else 0 end as value_on_hold,
    case when st.status in ('exception', 'needs_review')
         then date_diff('day', i.received_date, current_date) end as days_in_queue
from invoices as i
inner join status as st on i.invoice_id = st.invoice_id
inner join failed as f on i.invoice_id = f.invoice_id
inner join {{ ref('int_ap__invoice_supplier') }} as s on i.invoice_id = s.invoice_id
left join {{ ref('stg_ap__suppliers') }} as m on s.supplier_id = m.supplier_id
