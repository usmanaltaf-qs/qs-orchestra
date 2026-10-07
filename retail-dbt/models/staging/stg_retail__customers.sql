with source as (
    select * from {{ source('retail', 'customers') }}
)

select
    cast(customer_id as integer) as customer_id,
    trim(first_name) as first_name,
    trim(last_name) as last_name,
    lower(trim(email)) as email,
    {{ initcap('trim(city)') }} as city,
    region,
    cast(signup_date as date) as signup_date,
    loyalty_tier,
    marketing_opt_in
from source
