# NexusMart Data Analytics Pipeline (CodeConnect'26)

End-to-end pipeline: **raw CSV -> Python cleaning -> PostgreSQL star schema -> Apache Airflow -> Power BI dashboard**.
Period: Jan 2024 to Jun 2025, 9 CSV files, about 860,000 rows. All amounts in PKR.

## 1. Architecture
```
data/raw/*.csv
   |  extract, validate_raw           (Airflow tasks)
   v
scripts/01..03  Python cleaning  ->  data/clean/*.csv, data/rejects/*.csv, data/logs/*.csv
   |  load_staging
   v
PostgreSQL  staging.*  ->  dw.dim_date, dim_customer, dim_product, dim_seller, dim_location  +  dw.fact_sales
   |  build_dimensions, build_fact, quality_checks
   v
Power BI (nexusmart.pbix) reads the dw tables  ->  7 report pages  ->  executive summary
```
Star schema: one fact table (`dw.fact_sales`, one row per order line) and five dimensions that are not linked to each other. Diagram: `docs/star_schema_diagram.png`.

## 2. Project layout
| Path | Content |
|---|---|
| `scripts/common.py` | shared helpers and folder settings (env var `NEXUS_DATA_DIR`) |
| `scripts/01_clean_lookups.py` | locations, sellers, products, customers (incl. duplicate-customer merge) |
| `scripts/02_clean_orders.py` | orders and order items |
| `scripts/03_clean_payments_shipping_returns.py` | payments, shipping, returns |
| `scripts/warehouse.py` | creates the database, loads staging, builds dimensions and fact, quality checks |
| `sql/01..04_*.sql` | schema, dimensions, fact build, RFM segments |
| `dags/nexusmart_pipeline.py` | Airflow DAG (7 tasks) |
| `docker-compose.yml`, `Dockerfile` | Airflow + PostgreSQL environment |
| `docs/` | star schema diagram, data quality report, executive summary, dashboard PDF, .pbix |

## 3. Setup and run
Requirements: Docker Desktop (Option A) or Python 3.12 + PostgreSQL 16+ (Option B). Place the 9 raw CSV files in `data/raw/`. **Never edit them.**

1. Copy `.env.example` to `.env` and set your own `DB_PASSWORD` (never commit `.env`; credentials are never in code).

**Option A: Airflow in Docker (recommended)**
```
docker compose build
docker compose up airflow-init
docker compose up -d
```
Open http://localhost:8080 (login `admin` / `admin`) and trigger the DAG **nexusmart_pipeline**. Tasks run in order: `extract -> validate_raw -> clean -> load_staging -> build_dimensions -> build_fact -> quality_checks`. The warehouse is published on `localhost:5432` (change `WAREHOUSE_PORT` in `.env` if the port is busy), database `nexusmart`.

**Option B: run the scripts directly (no Docker)**
```
pip install pandas numpy sqlalchemy psycopg2-binary
cd scripts
python 01_clean_lookups.py
python 02_clean_orders.py
python 03_clean_payments_shipping_returns.py
python warehouse.py
```
(`warehouse.py` reads the `.env` file; with `DB_HOST=localhost` it creates the database `nexusmart` itself.)

**Power BI:** open `docs/nexusmart.pbix`, choose Home > Transform data > Data source settings, point the PostgreSQL source to your server (`localhost`) and database `nexusmart`, enter your credentials, then Refresh.

**Expected results after a run:** fact_sales 315,143 rows; NMV 1,199,300,393; net revenue 1,141,727,503; cancellation rate 9.16%; return rate 5.15%; 31,577 active customers.

## 4. Design decisions
- **Repeatable:** cleaning scripts overwrite their outputs; staging tables are replaced; dimensions are upserted on their business key (SCD Type 1); the fact table is rebuilt (TRUNCATE + INSERT). Running twice gives identical counts.
- **Nothing silently lost:** unfixable rows go to `data/rejects` with a reason and into `dw.reject_log`; every cleaning rule logs before/after counts into `data/logs` and `dw.cleaning_log`; each pipeline task logs records read, rejected and loaded to `dw.pipeline_log`.
- **Unknown members:** every dimension has a row with key -1 for broken or missing references.
- **Fail loudly:** `validate_raw` stops the run for missing files/columns, over 10% duplicates or over 5% broken links; `quality_checks` stops it if the fact table disagrees with the source.
- **Grain:** returns are summed per order line (valid returns only), and the per-order courier fee is split over the order's lines by value, so totals are never double counted.
- **Metrics** follow section 9 of the brief (NMV, Net Revenue, Return Rate, Cancellation Rate, Margin, AOV, ...) and are Power BI measures on `fact_sales`.

## 5. Cleaning rules (summary)
Full counts: `docs/data_quality_report.pdf`.
- Exact duplicates removed (3,200 orders, 3,296 payments). Duplicate customers merged only when name AND (e-mail OR phone) match; `customer_id_map.csv` keeps old IDs linked.
- Categories (38 spellings) -> 12 standard names; provinces, statuses, channels, couriers, payment methods standardised.
- Dates in three formats parsed (slashes are day/month/year); impossible order dates repaired from shipping date or neighbouring orders; rest quarantined/flagged.
- Text prices, words as quantities (`one`), negative signs and 0 / 9,999,999 placeholders repaired with documented rules; discounts recomputed from the order's discount code.
- Rejected returns (including `Declined`) are not valid returns. Refunds are capped at the price paid.

## 6. Known limitations and assumptions
- Missing ship city is filled with the customer's city (flagged); bad prices use list price x 1.026; missing courier fees use the courier median; quantity outliers in multi-line orders are set to 1.
- Products not in the catalogue (0.3% of lines) have no cost price, so margin on them is not meaningful; 25 cities have no province in the source.
- On-time delivery is defined as delivered within 4 days (75th percentile of delivery time).
- Seller Quality Score weights: revenue 30%, low return rate 30%, on-time rate 25%, low cancellation rate 15%.
- Findings in the executive summary show association, not proven cause.
