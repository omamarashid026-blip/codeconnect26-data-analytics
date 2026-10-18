"""NexusMart end-to-end pipeline.

extract >> validate_raw >> clean >> load_staging >> build_dimensions >> build_fact >> quality_checks

* Safe to run again and again (idempotent): cleaning rewrites its output files, staging tables are replaced,
  dimensions are upserted on their business key and the fact table is rebuilt from scratch.
* Database login and folder paths come from environment variables (see docker-compose.yml / .env), never from code.
* A task fails (and the whole run stops) when an important quality threshold is violated.
"""
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.exceptions import AirflowFailException
from airflow.operators.python import PythonOperator

PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", Path(__file__).resolve().parent.parent))
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DATA_DIR = Path(os.environ.get("NEXUS_DATA_DIR", PROJECT_DIR / "data"))
RAW_DIR = DATA_DIR / "raw"
sys.path.insert(0, str(SCRIPTS_DIR))

log = logging.getLogger(__name__)

# columns every raw file must contain (extra columns are allowed, missing ones stop the run)
REQUIRED_COLUMNS = {
    "customers": ["customer_id", "full_name", "email", "phone", "gender", "registration_date", "city_id", "birth_year"],
    "products": ["product_id", "product_name", "category", "sub_category", "brand", "list_price", "cost_price", "weight_kg", "seller_id"],
    "sellers": ["seller_id", "seller_name", "join_date", "seller_tier", "city_id", "rating"],
    "locations": ["city_id", "city", "province", "region", "latitude", "longitude", "population_tier"],
    "orders": ["order_id", "customer_id", "order_date", "order_status", "ship_city_id", "order_total", "discount_code", "channel"],
    "order_items": ["order_item_id", "order_id", "product_id", "seller_id", "quantity", "unit_price", "discount_amount"],
    "payments": ["payment_id", "order_id", "payment_method", "payment_status", "amount", "payment_date"],
    "shipping": ["shipment_id", "order_id", "courier", "shipped_date", "delivered_date", "shipping_fee", "delivery_status"],
    "returns": ["return_id", "order_item_id", "return_date", "return_reason", "return_status", "refund_amount"],
}
MAX_DUPLICATE_SHARE = 0.10      # stop if more than 10% of a file is exact duplicates
MAX_BROKEN_LINK_SHARE = 0.05    # stop if more than 5% of links point to missing rows


def _read_raw(name, **kw):
    import pandas as pd
    return pd.read_csv(RAW_DIR / f"{name}.csv", dtype=str, **kw)


def extract():
    """Read the raw CSV files and report how many records each one holds."""
    missing = [n for n in REQUIRED_COLUMNS if not (RAW_DIR / f"{n}.csv").exists()]
    if missing:
        raise AirflowFailException(f"Raw files missing in {RAW_DIR}: {missing}")
    total = 0
    for name in REQUIRED_COLUMNS:
        rows = len(_read_raw(name, usecols=[0]))
        total += rows
        log.info("extract: %-12s read=%d rejected=0 loaded=0", name, rows)
    log.info("extract: %d records read from %d files", total, len(REQUIRED_COLUMNS))


def validate_raw():
    """Check structure and first quality signals of the raw files; stop the run if something is badly wrong."""
    problems = []
    frames = {}
    for name, cols in REQUIRED_COLUMNS.items():
        df = _read_raw(name)
        frames[name] = df
        absent = [c for c in cols if c not in df.columns]
        extra = [c for c in df.columns if c not in cols]
        if absent:
            problems.append(f"{name}: missing columns {absent}")
        if extra:
            log.warning("validate_raw: %s has extra columns %s (allowed)", name, extra)
        if df.empty:
            problems.append(f"{name}: file is empty")
            continue
        dup = df.duplicated().sum()
        log.info("validate_raw: %-12s rows=%d exact_duplicates=%d", name, len(df), dup)
        if dup / len(df) > MAX_DUPLICATE_SHARE:
            problems.append(f"{name}: {dup / len(df):.1%} exact duplicates (limit {MAX_DUPLICATE_SHARE:.0%})")
    if not problems:
        links = [("order_items", "order_id", "orders", "order_id"), ("payments", "order_id", "orders", "order_id"),
                 ("shipping", "order_id", "orders", "order_id"), ("returns", "order_item_id", "order_items", "order_item_id"),
                 ("order_items", "product_id", "products", "product_id"), ("orders", "customer_id", "customers", "customer_id")]
        for child, ccol, parent, pcol in links:
            share = (~frames[child][ccol].isin(frames[parent][pcol])).mean()
            log.info("validate_raw: %s.%s -> %s.%s broken=%.2f%%", child, ccol, parent, pcol, share * 100)
            if share > MAX_BROKEN_LINK_SHARE:
                problems.append(f"{child}.{ccol}: {share:.1%} of links broken (limit {MAX_BROKEN_LINK_SHARE:.0%})")
    if problems:
        raise AirflowFailException("Raw data validation failed: " + "; ".join(problems))


def clean():
    """Run the three cleaning scripts in order (they log every fix with before/after counts)."""
    for script in ("01_clean_lookups.py", "02_clean_orders.py", "03_clean_payments_shipping_returns.py"):
        log.info("clean: running %s", script)
        result = subprocess.run([sys.executable, str(SCRIPTS_DIR / script)], cwd=str(SCRIPTS_DIR),
                                capture_output=True, text=True)
        for line in result.stdout.splitlines():
            log.info("%s | %s", script, line)
        if result.returncode != 0:
            raise AirflowFailException(f"{script} failed:\n{result.stderr[-2000:]}")


def _warehouse():
    import warehouse
    return warehouse, warehouse.engine()


def load_staging():
    warehouse, eng = _warehouse()
    warehouse.create_database()
    warehouse.load_staging(eng)


def build_dimensions():
    warehouse, eng = _warehouse()
    warehouse.build_dimensions(eng)


def build_fact():
    warehouse, eng = _warehouse()
    warehouse.build_fact(eng)


def quality_checks():
    warehouse, eng = _warehouse()
    try:
        warehouse.quality_checks(eng)
    except RuntimeError as e:
        raise AirflowFailException(str(e))


default_args = {"owner": "nexusmart", "retries": 2, "retry_delay": timedelta(minutes=1)}

with DAG(
    dag_id="nexusmart_pipeline",
    description="Raw CSV -> clean -> PostgreSQL staging -> star schema -> quality checks",
    default_args=default_args,
    start_date=datetime(2024, 1, 1),
    schedule=None,          # run on demand ("Trigger DAG"); change to "@daily" for a daily run
    catchup=False,
    max_active_runs=1,
    tags=["nexusmart", "datathon"],
) as dag:
    tasks = [PythonOperator(task_id=f.__name__, python_callable=f)
             for f in (extract, validate_raw, clean, load_staging, build_dimensions, build_fact, quality_checks)]
    for upstream, downstream in zip(tasks, tasks[1:]):
        upstream >> downstream
