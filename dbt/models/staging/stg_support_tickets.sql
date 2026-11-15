select
    ticket_id,
    cast(created_at as timestamp) as created_at,
    lower(customer_email) as customer_email,
    channel,
    subject,
    body
from {{ source('raw', 'support_tickets') }}
