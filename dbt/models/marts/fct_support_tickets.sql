-- A materialised copy of ticket text: a typical "forgotten" place PII ends up.
select * from {{ ref('stg_support_tickets') }}
