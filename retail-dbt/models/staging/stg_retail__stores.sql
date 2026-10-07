with source as (
    select * from {{ source('retail', 'stores') }}
)

select
    cast(store_id as integer) as store_id,
    store_name,
    {{ initcap('trim(city)') }} as city,
    region,
    store_type,
    cast(sq_ft as integer) as sq_ft,
    cast(opened_date as date) as opened_date
from source
