-- Dimensions are UPSERTED on their business key, so re-running never duplicates rows
-- and a changed attribute simply overwrites the old value (SCD Type 1).

-- ---------------------------------------------------------------- DimLocation
INSERT INTO dw.dim_location (location_key, city_id, city, province, region, population_tier)
VALUES (-1, '-1', 'Unknown', 'Unknown', 'Unknown', 'Unknown') ON CONFLICT (city_id) DO NOTHING;

INSERT INTO dw.dim_location (city_id, city, province, region, latitude, longitude, population_tier)
SELECT city_id, city, province, region, NULLIF(latitude,'')::numeric, NULLIF(longitude,'')::numeric, population_tier
FROM staging.stg_locations WHERE city_id <> '-1'
ON CONFLICT (city_id) DO UPDATE SET city = EXCLUDED.city, province = EXCLUDED.province, region = EXCLUDED.region,
    latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, population_tier = EXCLUDED.population_tier;

-- ------------------------------------------------------------------ DimSeller
INSERT INTO dw.dim_seller (seller_key, seller_id, seller_name, seller_tier, city, province, region)
VALUES (-1, 'S_UNKNOWN', 'Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown') ON CONFLICT (seller_id) DO NOTHING;

INSERT INTO dw.dim_seller (seller_id, seller_name, join_date, seller_tier, rating, city, province, region)
SELECT s.seller_id, s.seller_name, NULLIF(s.join_date,'')::date, s.seller_tier, NULLIF(s.rating,'')::numeric,
       COALESCE(l.city,'Unknown'), COALESCE(l.province,'Unknown'), COALESCE(l.region,'Unknown')
FROM staging.stg_sellers s LEFT JOIN staging.stg_locations l ON l.city_id = s.city_id
WHERE s.seller_id <> 'S_UNKNOWN'
ON CONFLICT (seller_id) DO UPDATE SET seller_name = EXCLUDED.seller_name, join_date = EXCLUDED.join_date,
    seller_tier = EXCLUDED.seller_tier, rating = EXCLUDED.rating, city = EXCLUDED.city,
    province = EXCLUDED.province, region = EXCLUDED.region;

-- ----------------------------------------------------------------- DimProduct
INSERT INTO dw.dim_product (product_key, product_id, product_name, category, sub_category, brand)
VALUES (-1, 'P_UNKNOWN', 'Unknown', 'Unknown', 'Unknown', 'Unknown') ON CONFLICT (product_id) DO NOTHING;

INSERT INTO dw.dim_product (product_id, product_name, category, sub_category, brand, list_price, cost_price, weight_kg)
SELECT product_id, product_name, category, sub_category, brand, NULLIF(list_price,'')::numeric,
       NULLIF(cost_price,'')::numeric, NULLIF(weight_kg,'')::numeric
FROM staging.stg_products WHERE product_id <> 'P_UNKNOWN'
ON CONFLICT (product_id) DO UPDATE SET product_name = EXCLUDED.product_name, category = EXCLUDED.category,
    sub_category = EXCLUDED.sub_category, brand = EXCLUDED.brand, list_price = EXCLUDED.list_price,
    cost_price = EXCLUDED.cost_price, weight_kg = EXCLUDED.weight_kg;

-- --------------------------------------------------------------- DimCustomer
INSERT INTO dw.dim_customer (customer_key, customer_id, full_name, gender, age_group, city, province, region, customer_segment)
VALUES (-1, 'C_UNKNOWN', 'Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown')
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO dw.dim_customer (customer_id, full_name, email, phone, gender, registration_date, birth_year,
                             age_group, city, province, region)
SELECT c.customer_id, c.full_name, c.email, c.phone, c.gender, NULLIF(c.registration_date,'')::date,
       NULLIF(c.birth_year,'')::numeric::int,
       CASE WHEN NULLIF(c.birth_year,'') IS NULL THEN 'Unknown'
            WHEN 2025 - c.birth_year::numeric::int < 25 THEN 'Under 25'
            WHEN 2025 - c.birth_year::numeric::int < 35 THEN '25-34'
            WHEN 2025 - c.birth_year::numeric::int < 45 THEN '35-44'
            WHEN 2025 - c.birth_year::numeric::int < 55 THEN '45-54'
            ELSE '55+' END,
       COALESCE(l.city,'Unknown'), COALESCE(l.province,'Unknown'), COALESCE(l.region,'Unknown')
FROM staging.stg_customers c LEFT JOIN staging.stg_locations l ON l.city_id = c.city_id
WHERE c.customer_id <> 'C_UNKNOWN'
ON CONFLICT (customer_id) DO UPDATE SET full_name = EXCLUDED.full_name, email = EXCLUDED.email,
    phone = EXCLUDED.phone, gender = EXCLUDED.gender, registration_date = EXCLUDED.registration_date,
    birth_year = EXCLUDED.birth_year, age_group = EXCLUDED.age_group, city = EXCLUDED.city,
    province = EXCLUDED.province, region = EXCLUDED.region;

-- ------------------------------------------------------------------- DimDate
INSERT INTO dw.dim_date (date_key, event_name) VALUES (-1, 'Unknown') ON CONFLICT (date_key) DO NOTHING;

INSERT INTO dw.dim_date
SELECT to_char(d, 'YYYYMMDD')::int, d, EXTRACT(day FROM d)::int, trim(to_char(d, 'Day')), EXTRACT(week FROM d)::int,
       EXTRACT(month FROM d)::int, trim(to_char(d, 'Month')), to_char(d, 'YYYY-MM'), EXTRACT(quarter FROM d)::int,
       EXTRACT(year FROM d)::int, EXTRACT(isodow FROM d) IN (6, 7),
       (d BETWEEN '2024-03-11' AND '2024-04-09') OR (d BETWEEN '2025-03-01' AND '2025-03-30'),
       (d BETWEEN '2024-04-10' AND '2024-04-12') OR (d BETWEEN '2025-03-31' AND '2025-04-02'),
       (d BETWEEN '2024-06-17' AND '2024-06-19') OR (d BETWEEN '2025-06-07' AND '2025-06-09'),
       (EXTRACT(month FROM d) = 8 AND EXTRACT(day FROM d) = 14),
       (EXTRACT(month FROM d) = 11 AND EXTRACT(day FROM d) = 11),
       (d = '2024-11-29'),
       FALSE, 'Regular day'
FROM generate_series('2024-01-01'::date, '2025-12-31'::date, interval '1 day') AS g(d)
ON CONFLICT (date_key) DO NOTHING;

UPDATE dw.dim_date SET is_holiday = (is_eid_ul_fitr OR is_eid_ul_adha OR is_independence_day),
    event_name = CASE WHEN is_eid_ul_fitr THEN 'Eid-ul-Fitr' WHEN is_eid_ul_adha THEN 'Eid-ul-Adha'
                      WHEN is_ramadan THEN 'Ramadan' WHEN is_independence_day THEN '14 August'
                      WHEN is_11_11 THEN '11.11' WHEN is_black_friday THEN 'Black Friday'
                      ELSE 'Regular day' END
WHERE date_key <> -1;
