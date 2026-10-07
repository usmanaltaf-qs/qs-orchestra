-- Physical stores plus an 'Online' row (store_id = 0) so online sales join cleanly.
with stores as (
    select * from {{ ref('stg_retail__stores') }}
)

select
    store_id,
    store_name,
    city,
    region,
    store_type,
    sq_ft,
    opened_date,
    false as is_online
from stores

union all

select
    0 as store_id,
    'Online' as store_name,
    cast(null as varchar) as city,
    'Online' as region,
    'online' as store_type,
    cast(null as integer) as sq_ft,
    cast(null as date) as opened_date,
    true as is_online
