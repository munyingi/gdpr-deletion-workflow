select
    event_id,
    customer_id,
    cast(event_ts as timestamp) as event_ts,
    page,
    ip_address,
    user_agent
from {{ source('raw', 'web_events') }}
