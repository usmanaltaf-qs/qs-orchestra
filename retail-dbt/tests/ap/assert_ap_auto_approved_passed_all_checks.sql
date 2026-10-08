-- Every auto_approved invoice passed every applicable check.
select s.invoice_id, c.check_name
from {{ ref('fct_invoice_status') }} as s
inner join {{ ref('int_ap__invoice_checks') }} as c on s.invoice_id = c.invoice_id
where s.status = 'auto_approved'
  and c.passed = false
