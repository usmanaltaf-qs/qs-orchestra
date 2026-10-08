{{ config(enabled=target.name == 'dev', severity='error') }}
-- Dev only: rules vs the generator's ground truth. Fails for every injected problem that wasn't
-- caught and every check that failed without an injected problem behind it.
-- Line-level problems (price_variance, qty_over_received, extra_charge) are masked when the
-- invoice also has missing_po or wrong_po: there's no valid PO to compare lines against, and
-- the invoice is held as an exception anyway.
-- Assumes extraction was correct. A failure here after a real extraction can be an
-- extraction error; the matching eval (evals/run_evals.py --suite invoices-matching) feeds
-- ground truth in as the extraction to test the rules alone.
with truth as (
    select file, unnest(injected_problems) as problem, injected_problems
    from read_json_auto('{{ env_var("INVOICES_SOURCE_URI", "../data/invoices/dev") }}/_truth/*.json')
),

problem_check(problem, check_name, line_level) as (
    values
        ('price_variance', 'price_variance', true),
        ('qty_over_received', 'qty_over_received', true),
        ('extra_charge', 'unmatched_line', true),
        ('missing_po', 'missing_po', false),
        ('wrong_po', 'po_supplier_mismatch', false),
        ('duplicate', 'duplicate', false),
        ('arithmetic_error', 'arithmetic', false),
        ('unknown_supplier', 'unknown_supplier', false)
),

expected as (
    select s.invoice_id, pc.check_name
    from truth as t
    inner join problem_check as pc on t.problem = pc.problem
    inner join {{ ref('fct_invoice_status') }} as s on t.file = s.file_path
    where not (pc.line_level and (list_contains(t.injected_problems, 'missing_po')
                                  or list_contains(t.injected_problems, 'wrong_po')))
),

actual as (
    select c.invoice_id, c.check_name
    from {{ ref('int_ap__invoice_checks') }} as c
    where c.passed = false
)

select coalesce(e.invoice_id, a.invoice_id) as invoice_id,
       coalesce(e.check_name, a.check_name) as check_name,
       case when a.invoice_id is null then 'missed' else 'false_positive' end as problem
from expected as e
full outer join actual as a on e.invoice_id = a.invoice_id and e.check_name = a.check_name
where e.invoice_id is null or a.invoice_id is null
