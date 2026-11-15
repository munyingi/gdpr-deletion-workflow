select
    order_id,
    customer_id,
    cast(order_ts as timestamp) as order_ts,
    cast(amount_eur as decimal(10, 2)) as amount_eur,
    items,
    customer_email,
    shipping_name,
    shipping_address
from {{ source('raw', 'orders') }}
