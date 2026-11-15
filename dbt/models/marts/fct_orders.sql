-- Order facts with the customer's segment at the time of the order (point-in-time SCD2 join).
select
    o.order_id,
    o.customer_id,
    o.order_ts,
    o.amount_eur,
    o.items,
    c.segment as segment_at_order,
    c.city    as city_at_order
from {{ ref('stg_orders') }} o
left join {{ ref('dim_customers') }} c
    on o.customer_id = c.customer_id
   and o.order_ts >= c.valid_from
   and (c.valid_to is null or o.order_ts < c.valid_to)
