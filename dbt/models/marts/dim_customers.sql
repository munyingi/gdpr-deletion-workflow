-- SCD2 customer dimension built on the snapshot. One row per version.
select
    customer_id,
    first_name,
    last_name,
    email,
    phone,
    city,
    segment,
    marketing_opt_in,
    dbt_valid_from                                   as valid_from,
    dbt_valid_to                                     as valid_to,
    dbt_valid_to is null                             as is_current,
    row_number() over (partition by customer_id order by dbt_valid_from) as version
from {{ ref('customers_snapshot') }}
