with source as (
    select * from {{ source('ap', 'suppliers') }}
)

select
    supplier_id,
    supplier_name,
    {{ ap_norm_name('supplier_name') }} as supplier_name_norm,
    {{ ap_norm_id('vat_number') }} as vat_number,
    {{ ap_digits('bank_sort_code') }} as bank_sort_code,
    {{ ap_digits('bank_account') }} as bank_account,
    cast(payment_terms_days as integer) as payment_terms_days
from source
