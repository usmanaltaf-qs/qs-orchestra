-- Invoice lines matched to lines of the PO printed on the invoice: on SKU when the invoice line
-- has one, otherwise the most similar description on that PO (jaro-winkler >= var). Then joined
-- to quantities received (all GRNs for the PO line) and the product's category.
-- Ties (e.g. two products with the same name on one PO, and no SKU column on the invoice) go to
-- the PO line with the closest unit price, then quantity, then line position.
-- Unmatched lines (e.g. a delivery charge) keep NULL PO columns.
with invoices as (
    select invoice_id, file_hash, po_number from {{ ref('int_ap__invoices') }}
),

lines as (
    select i.invoice_id || '-' || cast(l.invoice_line_no as varchar) as invoice_line_id, i.invoice_id, l.*
    from {{ ref('stg_ap__extraction_lines') }} as l
    inner join invoices as i on l.file_hash = i.file_hash
),

po_lines as (
    select * from {{ ref('stg_ap__po_lines') }}
),

candidates as (
    select
        l.invoice_line_id,
        pl.po_line_id,
        case when l.sku is not null then 'sku' else 'description' end as po_match_method,
        case
            when l.sku is not null then 1.0
            else jaro_winkler_similarity(l.description_norm, pl.description_norm)
        end as match_score,
        abs(pl.unit_cost - l.unit_price) as price_gap,
        abs(pl.qty_ordered - l.quantity) as qty_gap,
        abs(pl.po_line_no - l.invoice_line_no) as position_gap
    from lines as l
    inner join invoices as i on l.invoice_id = i.invoice_id
    inner join po_lines as pl on i.po_number = pl.po_number
    where (l.sku is not null and l.sku = pl.sku)
       or (l.sku is null
           and jaro_winkler_similarity(l.description_norm, pl.description_norm)
               >= {{ var('ap_description_similarity') }})
),

best as (
    select *
    from candidates
    qualify row_number() over (
        partition by invoice_line_id
        order by match_score desc, price_gap, qty_gap, position_gap, po_line_id
    ) = 1
),

received as (
    select po_number, po_line_no, sum(qty_received) as qty_received
    from {{ ref('stg_ap__grn_lines') }}
    group by po_number, po_line_no
)

select
    l.invoice_line_id,
    l.invoice_id,
    l.invoice_line_no,
    l.description,
    l.sku,
    l.quantity,
    l.unit_price,
    l.vat_rate,
    l.line_net,
    b.po_line_id,
    b.po_match_method,
    b.match_score,
    pl.po_number,
    pl.po_line_no,
    pl.product_id,
    pl.qty_ordered,
    pl.unit_cost as po_unit_cost,
    r.qty_received,
    p.category as product_category
from lines as l
left join best as b on l.invoice_line_id = b.invoice_line_id
left join po_lines as pl on b.po_line_id = pl.po_line_id
left join received as r on pl.po_number = r.po_number and pl.po_line_no = r.po_line_no
left join {{ ref('stg_retail__products') }} as p on pl.product_id = p.product_id
