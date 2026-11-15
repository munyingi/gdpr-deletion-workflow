{% snapshot customers_snapshot %}
{#
  SCD Type 2 history of the CRM customer record. Each nightly extract that
  changes a customer closes the old version and opens a new one, so old
  emails, phones and addresses live on here long after the CRM drops them.
  That is exactly why erasure has to reach into this table.
#}
{{ config(
    target_schema='snapshots',
    unique_key='customer_id',
    strategy='timestamp',
    updated_at='updated_at'
) }}
select
    customer_id, first_name, last_name, email, phone,
    street_address, postal_code, city,
    cast(date_of_birth as date)        as date_of_birth,
    cast(marketing_opt_in as boolean)  as marketing_opt_in,
    segment,
    cast(updated_at as timestamp)      as updated_at
from {{ source('raw', 'customers') }}
{% endsnapshot %}
