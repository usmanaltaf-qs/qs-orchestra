-- One row per inbox file with its (latest) extraction. Files not extracted yet are left out.
select
    f.invoice_id,
    f.file_path,
    f.received_date,
    f.first_seen_at,
    e.*
from {{ ref('stg_ap__inbox_files') }} as f
inner join {{ ref('stg_ap__extractions') }} as e on f.file_hash = e.file_hash
