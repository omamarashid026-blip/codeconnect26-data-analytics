-- FactSales: one row per order line. Rebuilt from scratch on every run (TRUNCATE + INSERT),
-- so running the pipeline twice gives exactly the same rows (idempotent).
TRUNCATE dw.fact_sales;

INSERT INTO dw.fact_sales (date_key, customer_key, product_key, seller_key, location_key, order_id, order_item_id,
    quantity, unit_price, gross_amount, discount_amount, net_amount, cost_amount, shipping_fee, shipping_days,
    is_cancelled, is_delivered, is_returned, returned_amount, return_date_key, return_reason, return_status,
    order_status, payment_method, channel)
WITH line AS (
    SELECT oi.order_item_id, oi.order_id, oi.product_id, oi.seller_id,
           oi.quantity::numeric::int AS qty, oi.unit_price::numeric AS unit_price,
           oi.gross_amount::numeric AS gross, oi.discount_amount::numeric AS disc,
           o.customer_id, o.order_date::date AS order_date, o.ship_city_id, o.order_status, o.channel,
           SUM(oi.gross_amount::numeric - oi.discount_amount::numeric) OVER (PARTITION BY oi.order_id) AS order_net,
           COUNT(*) OVER (PARTITION BY oi.order_id) AS n_lines
    FROM staging.stg_order_items oi
    JOIN staging.stg_orders o ON o.order_id = oi.order_id
),
valid_returns AS (   -- Rejected returns are ignored; the rest are summed per order line (keeps the grain)
    SELECT order_item_id, SUM(refund_amount::numeric) AS refund, MIN(return_date::date) AS return_date,
           (array_agg(return_reason ORDER BY refund_amount::numeric DESC))[1] AS reason,
           (array_agg(return_status ORDER BY refund_amount::numeric DESC))[1] AS status
    FROM staging.stg_returns WHERE is_valid_return = 'True'
    GROUP BY order_item_id
)
SELECT COALESCE(d.date_key, -1), COALESCE(c.customer_key, -1), COALESCE(p.product_key, -1),
       COALESCE(s.seller_key, -1), COALESCE(loc.location_key, -1),
       l.order_id, l.order_item_id, l.qty, l.unit_price, l.gross, l.disc, l.gross - l.disc,
       l.qty * p.cost_price,
       -- the courier fee belongs to the whole order: split it over the lines by value so it is never double counted
       ROUND(NULLIF(sh.shipping_fee,'')::numeric *
             CASE WHEN l.order_net > 0 THEN (l.gross - l.disc) / l.order_net ELSE 1.0 / l.n_lines END, 2),
       NULLIF(sh.shipping_days,'')::numeric::int,
       l.order_status = 'Cancelled',
       l.order_status NOT IN ('Cancelled', 'In Transit'),
       r.order_item_id IS NOT NULL, COALESCE(r.refund, 0), to_char(r.return_date, 'YYYYMMDD')::int,
       r.reason, r.status,
       l.order_status, COALESCE(pay.payment_method, 'Unknown'), l.channel
FROM line l
LEFT JOIN dw.dim_date d        ON d.full_date = l.order_date
LEFT JOIN dw.dim_customer c    ON c.customer_id = l.customer_id
LEFT JOIN dw.dim_product p     ON p.product_id = l.product_id
LEFT JOIN dw.dim_seller s      ON s.seller_id = l.seller_id
LEFT JOIN dw.dim_location loc  ON loc.city_id = l.ship_city_id
LEFT JOIN staging.stg_shipping sh       ON sh.order_id = l.order_id
LEFT JOIN staging.stg_order_payment pay ON pay.order_id = l.order_id
LEFT JOIN valid_returns r      ON r.order_item_id = l.order_item_id;
