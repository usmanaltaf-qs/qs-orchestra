-- One row per invoice x check. passed: true/false, or NULL when the check doesn't apply
-- (e.g. line checks when there's no valid PO to compare against). expected/actual/detail are
-- short strings for reviewers and the explanation step. Rules decide here; no LLM involved.
{% set pct = var('ap_price_tolerance_pct') %}
{% set abs_tol = var('ap_price_tolerance_abs') %}
{% set tol = var('ap_amount_tolerance') %}

with invoices as (
    select
        i.*,
        s.supplier_id,
        s.supplier_match_method,
        m.bank_sort_code as master_sort_code,
        m.bank_account as master_account,
        po.po_number as po_found,
        po.supplier_id as po_supplier_id,
        -- line-level checks only make sense against the supplier's own PO
        (po.po_number is not null and s.supplier_id is not null and po.supplier_id = s.supplier_id) as has_valid_po
    from {{ ref('int_ap__invoices') }} as i
    inner join {{ ref('int_ap__invoice_supplier') }} as s on i.invoice_id = s.invoice_id
    left join {{ ref('stg_ap__suppliers') }} as m on s.supplier_id = m.supplier_id
    left join {{ ref('stg_ap__purchase_orders') }} as po on i.po_number = po.po_number
),

lines as (
    select
        l.*,
        i.has_valid_po,
        l.po_line_id is not null
            and abs(l.unit_price - l.po_unit_cost) > l.po_unit_cost * {{ pct }}
            and abs(l.unit_price - l.po_unit_cost) > {{ abs_tol }} as is_price_variance,
        l.po_line_id is not null and l.quantity > coalesce(l.qty_received, 0) as is_over_received,
        l.po_line_id is not null
            and l.vat_rate <> case when l.product_category = 'Food' then 0.00 else 0.20 end as is_wrong_vat_rate,
        abs(l.quantity * l.unit_price - l.line_net) > {{ tol }} as is_line_arithmetic_error
    from {{ ref('int_ap__invoice_lines_matched') }} as l
    inner join invoices as i on l.invoice_id = i.invoice_id
),

line_agg as (
    select
        invoice_id,
        count(*) as n_lines,
        count(po_line_id) as n_matched,
        sum(line_net) as sum_line_net,
        count(*) filter (where is_price_variance) as n_price_variance,
        count(*) filter (where is_over_received) as n_over_received,
        count(*) filter (where po_line_id is null) as n_unmatched,
        count(*) filter (where is_wrong_vat_rate) as n_wrong_vat,
        count(*) filter (where is_line_arithmetic_error) as n_line_arith,
        string_agg('line ' || invoice_line_no || ': ' || cast(po_unit_cost as varchar), '; ' order by invoice_line_no)
            filter (where is_price_variance) as pv_expected,
        string_agg('line ' || invoice_line_no || ': ' || cast(unit_price as varchar), '; ' order by invoice_line_no)
            filter (where is_price_variance) as pv_actual,
        string_agg('line ' || invoice_line_no || ': received ' || cast(coalesce(qty_received, 0) as varchar), '; '
                   order by invoice_line_no) filter (where is_over_received) as or_expected,
        string_agg('line ' || invoice_line_no || ': invoiced ' || cast(quantity as varchar), '; '
                   order by invoice_line_no) filter (where is_over_received) as or_actual,
        string_agg('line ' || invoice_line_no || ': ' || coalesce(description, '?') || ' '
                   || cast(line_net as varchar), '; ' order by invoice_line_no)
            filter (where po_line_id is null) as unmatched_actual,
        string_agg('line ' || invoice_line_no || ': ' || cast(vat_rate as varchar), '; ' order by invoice_line_no)
            filter (where is_wrong_vat_rate) as vat_actual,
        string_agg('line ' || invoice_line_no || ': ' || cast(quantity as varchar) || ' x '
                   || cast(unit_price as varchar) || ' = ' || cast(round(quantity * unit_price, 2) as varchar)
                   || ', printed ' || cast(line_net as varchar), '; ' order by invoice_line_no)
            filter (where is_line_arithmetic_error) as line_arith_detail
    from lines
    group by invoice_id
),

vat_by_rate as (
    select invoice_id, sum(round(rate_net * vat_rate, 2)) as expected_vat
    from (
        select invoice_id, vat_rate, sum(line_net) as rate_net
        from lines
        group by invoice_id, vat_rate
    )
    group by invoice_id
),

-- duplicate: an earlier file with the same supplier + invoice number, or the same bytes
dupes as (
    select
        invoice_id,
        (invoice_number is not null and row_number() over by_number > 1)
            or row_number() over by_hash > 1 as is_duplicate,
        case when row_number() over by_hash > 1 then first_value(file_path) over by_hash
             else first_value(file_path) over by_number end as first_file_path
    from invoices
    window
        -- same-day tie: a document marked COPY is the re-send
        by_number as (partition by coalesce(supplier_id, supplier_name_norm), invoice_number
                      order by received_date, is_copy_or_duplicate_marked, first_seen_at, file_path
                      rows between unbounded preceding and unbounded following),
        by_hash as (partition by file_hash order by received_date, first_seen_at, file_path
                    rows between unbounded preceding and unbounded following)
),

base as (
    select
        i.*,
        a.n_lines, a.n_matched, a.sum_line_net, a.n_price_variance, a.n_over_received, a.n_unmatched,
        a.n_wrong_vat, a.n_line_arith, a.pv_expected, a.pv_actual, a.or_expected, a.or_actual,
        a.unmatched_actual, a.vat_actual, a.line_arith_detail,
        v.expected_vat,
        d.is_duplicate, d.first_file_path
    from invoices as i
    left join line_agg as a on i.invoice_id = a.invoice_id
    left join vat_by_rate as v on i.invoice_id = v.invoice_id
    left join dupes as d on i.invoice_id = d.invoice_id
),

checks as (
    select invoice_id, 'duplicate' as check_name,
        not is_duplicate as passed,
        'first invoice with this supplier and number' as expected,
        case when is_duplicate then 'already received in ' || first_file_path end as actual,
        'invoice ' || coalesce(invoice_number, '?') as detail
    from base

    union all
    select invoice_id, 'unknown_supplier',
        supplier_id is not null,
        'supplier in master data',
        coalesce(supplier_name, '?') || ' / VAT ' || coalesce(supplier_vat_number, 'none'),
        case when supplier_match_method is not null then 'matched on ' || supplier_match_method end
    from base

    union all
    select invoice_id, 'missing_po',
        po_number is not null,
        'PO number on invoice', po_number, null
    from base

    union all
    select invoice_id, 'po_not_found',
        case when po_number is null then null else po_found is not null end,
        'PO exists', po_number, null
    from base

    union all
    select invoice_id, 'po_supplier_mismatch',
        case when po_found is null or supplier_id is null then null else po_supplier_id = supplier_id end,
        supplier_id, po_supplier_id, 'PO ' || po_number || ' belongs to ' || coalesce(po_supplier_id, '?')
    from base

    union all
    select invoice_id, 'price_variance',
        case when not has_valid_po or n_matched = 0 then null else n_price_variance = 0 end,
        pv_expected, pv_actual,
        'tolerance ' || cast({{ pct }} * 100 as varchar) || '% and £' || '{{ abs_tol }}'
    from base

    union all
    select invoice_id, 'qty_over_received',
        case when not has_valid_po or n_matched = 0 then null else n_over_received = 0 end,
        or_expected, or_actual, null
    from base

    union all
    select invoice_id, 'unmatched_line',
        case when not has_valid_po or n_lines is null then null else n_unmatched = 0 end,
        'every line on the PO', unmatched_actual, null
    from base

    union all
    select invoice_id, 'arithmetic',
        case when n_lines is null then null else
            n_line_arith = 0
            and abs(sum_line_net - subtotal_net) <= {{ tol }}
            and abs(expected_vat - vat_total) <= {{ tol }}
            and abs(subtotal_net + vat_total - total_gross) <= {{ tol }}
        end,
        'subtotal ' || cast(sum_line_net as varchar) || ', VAT ' || cast(expected_vat as varchar)
            || ', total ' || cast(subtotal_net + vat_total as varchar),
        'subtotal ' || cast(subtotal_net as varchar) || ', VAT ' || cast(vat_total as varchar)
            || ', total ' || cast(total_gross as varchar),
        line_arith_detail
    from base

    union all
    select invoice_id, 'vat_rate',
        case when n_matched is null or n_matched = 0 then null else n_wrong_vat = 0 end,
        '0% for Food, 20% otherwise', vat_actual, null
    from base

    union all
    select invoice_id, 'bank_details_changed',
        case when supplier_id is null or (bank_sort_code is null and bank_account is null) then null
             else coalesce(bank_sort_code = master_sort_code, true) and coalesce(bank_account = master_account, true)
        end,
        'sort code ' || coalesce(master_sort_code, '?') || ', account ' || coalesce(master_account, '?'),
        'sort code ' || coalesce(bank_sort_code, '?') || ', account ' || coalesce(bank_account, '?'),
        null
    from base

    union all
    select invoice_id, 'extraction',
        extraction_status = 'ok',
        'ok', extraction_status, extraction_error
    from base
)

select
    invoice_id || '-' || check_name as invoice_check_id,
    invoice_id,
    check_name,
    passed,
    expected,
    actual,
    detail
from checks
