-- Fills the customer columns that need the fact table: first order date and RFM segment.
-- RFM = Recency (days since last order, measured at 30 June 2025), Frequency (orders), Monetary (net spend).
WITH orders_per_customer AS (
    SELECT f.customer_key, MIN(d.full_date) AS first_order, MAX(d.full_date) AS last_order,
           COUNT(DISTINCT f.order_id) AS frequency, SUM(f.net_amount) AS monetary
    FROM dw.fact_sales f JOIN dw.dim_date d ON d.date_key = f.date_key
    WHERE NOT f.is_cancelled AND f.customer_key <> -1
    GROUP BY f.customer_key
)
UPDATE dw.dim_customer c SET first_order_date = o.first_order
FROM orders_per_customer o WHERE c.customer_key = o.customer_key;

WITH scored AS (
    SELECT f.customer_key,
           DATE '2025-06-30' - MAX(d.full_date) AS recency_days,
           COUNT(DISTINCT f.order_id) AS frequency,
           SUM(f.net_amount) AS monetary
    FROM dw.fact_sales f JOIN dw.dim_date d ON d.date_key = f.date_key
    WHERE NOT f.is_cancelled AND f.customer_key <> -1
    GROUP BY f.customer_key
), rated AS (
    SELECT customer_key,
           CASE WHEN recency_days <= 60 THEN 5 WHEN recency_days <= 120 THEN 4 WHEN recency_days <= 240 THEN 3
                WHEN recency_days <= 360 THEN 2 ELSE 1 END AS r,
           NTILE(5) OVER (ORDER BY frequency, monetary) AS f,
           NTILE(5) OVER (ORDER BY monetary) AS m
    FROM scored
)
UPDATE dw.dim_customer c SET customer_segment =
    CASE WHEN r >= 4 AND f >= 4 THEN 'Champions'
         WHEN r >= 3 AND f >= 4 THEN 'Loyal Customers'
         WHEN r >= 4 AND f <= 2 THEN 'New / Recent Customers'
         WHEN r >= 3 AND f = 3 THEN 'Potential Loyalists'
         WHEN r <= 2 AND f >= 4 THEN 'Cannot Lose Them'
         WHEN r <= 2 AND f = 3 THEN 'At Risk'
         WHEN r <= 2 AND f <= 2 THEN 'Hibernating'
         ELSE 'Need Attention' END
FROM rated x WHERE c.customer_key = x.customer_key;

UPDATE dw.dim_customer SET customer_segment = 'No Purchases'
WHERE customer_key <> -1 AND customer_segment IS NULL;
