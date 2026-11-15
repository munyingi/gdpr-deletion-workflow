select
    o.customer_id,
    any_value(c.email)          as email,
    any_value(c.segment)        as current_segment,
    count(*)                    as orders,
    sum(o.amount_eur)           as revenue_eur,
    min(o.order_ts)             as first_order_ts,
    max(o.order_ts)             as last_order_ts
from {{ ref('stg_orders') }} o
left join {{ ref('dim_customers') }} c
    on o.customer_id = c.customer_id and c.is_current
group by o.customer_id
