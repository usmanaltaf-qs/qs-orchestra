-- One row per day between the date_spine_start/end vars, with calendar and retail-season
-- attributes. UK retail seasons: Black Friday week (Mon before Black Friday to Cyber
-- Monday), Christmas (1-25 Dec), January sale (26 Dec - 31 Jan).
with spine as (
    {{ dbt_utils.date_spine(
        datepart="day",
        start_date="cast('" ~ var('date_spine_start') ~ "' as date)",
        end_date="cast('" ~ var('date_spine_end') ~ "' as date)"
    ) }}
),

days as (
    select
        cast(date_day as date) as date_day,
        cast(cast(extract(year from date_day) as varchar) || '-11-01' as date) as nov_1
    from spine
),

with_black_friday as (
    select
        date_day,
        -- Black Friday = day after the 4th Thursday of November (dow: Sunday = 0)
        cast({{ dbt.dateadd('day', '(4 - extract(dow from nov_1) + 7) % 7 + 22', 'nov_1') }} as date)
            as black_friday
    from days
)

select
    date_day,
    cast(extract(year from date_day) as integer) as year,
    cast(extract(quarter from date_day) as integer) as quarter,
    cast(extract(month from date_day) as integer) as month,
    monthname(date_day) as month_name,
    cast({{ dbt.date_trunc('month', 'date_day') }} as date) as month_start,
    cast(extract(isoyear from date_day) as integer) as iso_year,
    cast(extract(week from date_day) as integer) as iso_week,
    cast({{ dbt.date_trunc('week', 'date_day') }} as date) as week_start,
    cast(extract(isodow from date_day) as integer) as day_of_week,
    dayname(date_day) as day_name,
    extract(isodow from date_day) in (6, 7) as is_weekend,
    case
        when date_day between cast({{ dbt.dateadd('day', -4, 'black_friday') }} as date)
                          and cast({{ dbt.dateadd('day', 3, 'black_friday') }} as date)
            then 'Black Friday week'
        when extract(month from date_day) = 12 and extract(day from date_day) <= 25 then 'Christmas'
        when extract(month from date_day) = 12 or extract(month from date_day) = 1 then 'January sale'
        else 'Regular'
    end as retail_season
from with_black_friday
