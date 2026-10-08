-- One row per inbox PDF path. invoice_id is derived from the path: a re-sent document is a
-- separate invoice even when its bytes match an earlier one.
with source as (
    select * from {{ source('ap', 'inbox_files') }}
)

select
    md5(file_path) as invoice_id,
    file_path,
    file_hash,
    cast(strptime(regexp_extract(file_path, 'inbox/(\d{4}/\d{2}/\d{2})/', 1), '%Y/%m/%d') as date) as received_date,
    first_seen_at
from source
qualify row_number() over (partition by file_path order by first_seen_at) = 1
