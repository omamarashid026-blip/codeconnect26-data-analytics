"""Loads the cleaned data into PostgreSQL and builds the star schema.

Run it on its own:   python scripts/warehouse.py
Airflow imports the functions below, one task per function.

Database settings are NEVER written in the code. They come from environment variables or a
.env file in the project folder (see .env.example):
    DB_HOST  DB_PORT  DB_USER  DB_PASSWORD  DB_NAME
"""
import getpass
import io
import os
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from common import CLEAN, LOGS, REJECTS

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = Path(os.environ.get("NEXUS_SQL_DIR", ROOT / "sql"))

# clean CSV file  ->  staging table
STAGING = {
    "locations": "stg_locations", "sellers": "stg_sellers", "products": "stg_products",
    "customers": "stg_customers", "orders": "stg_orders", "order_items": "stg_order_items",
    "order_payment": "stg_order_payment", "shipping": "stg_shipping", "returns": "stg_returns",
}


# ------------------------------------------------------------ connection
def _read_env_file():
    values, f = {}, ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def config():
    env = {**_read_env_file(), **os.environ}          # real environment variables win over the file
    cfg = {"host": env.get("DB_HOST", "localhost"), "port": int(env.get("DB_PORT", 5432)),
           "user": env.get("DB_USER", "postgres"), "password": env.get("DB_PASSWORD"),
           "name": env.get("DB_NAME", "nexusmart")}
    if cfg["password"] is None and sys.stdin.isatty():   # typing it in is also fine (never saved)
        cfg["password"] = getpass.getpass(f"PostgreSQL password for user {cfg['user']}: ")
    return cfg


def engine(dbname=None):
    c = config()
    url = URL.create("postgresql+psycopg2", username=c["user"], password=c["password"] or None,
                     host=c["host"], port=c["port"], database=dbname or c["name"])
    return create_engine(url, isolation_level="AUTOCOMMIT")


def run_sql_file(eng, name):
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    raw = eng.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    print(f"  ran {name}")


def scalar(eng, sql):
    with eng.connect() as c:
        v = c.execute(text(sql)).scalar()
    return float(v) if hasattr(v, "as_tuple") else v   # Decimal -> float, so sums can be compared


def log_step(eng, task, detail, read=None, rejected=None, loaded=None):
    print(f"[{task}] {detail}  read={read} rejected={rejected} loaded={loaded}")
    with eng.connect() as c:
        c.execute(text("INSERT INTO dw.pipeline_log (task, detail, rows_read, rows_rejected, rows_loaded) "
                       "VALUES (:t, :d, :r, :j, :l)"), {"t": task, "d": detail, "r": read, "j": rejected, "l": loaded})


def copy_df(eng, df, table):
    """Fast bulk load with PostgreSQL COPY."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    raw = eng.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT csv, NULL '')", buf)
        raw.commit()
    finally:
        raw.close()


def read_csv(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


# ------------------------------------------------------------ pipeline tasks
def create_database():
    c = config()
    admin = engine("postgres")
    exists = scalar(admin, f"SELECT 1 FROM pg_database WHERE datname = '{c['name']}'")
    if not exists:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{c["name"]}"'))
        print(f"  created database {c['name']}")
    eng = engine()
    run_sql_file(eng, "01_create_schema.sql")
    return eng


def load_staging(eng):
    """Copy every cleaned CSV into a staging table (replaced each run, so no duplicates)."""
    for csv_name, table in STAGING.items():
        df = read_csv(CLEAN / f"{csv_name}.csv")
        cols = ", ".join(f'"{c}" TEXT' for c in df.columns)
        with eng.connect() as c:
            c.execute(text(f"DROP TABLE IF EXISTS staging.{table}"))
            c.execute(text(f"CREATE TABLE staging.{table} ({cols})"))
        copy_df(eng, df, f"staging.{table}")
        log_step(eng, "load_staging", table, read=len(df), rejected=0, loaded=scalar(eng, f"SELECT count(*) FROM staging.{table}"))
    # rejected records and the cleaning log are stored too, so nothing is silently lost
    with eng.connect() as c:
        c.execute(text("TRUNCATE dw.reject_log, dw.cleaning_log"))
    n_rej = 0
    for f in sorted(REJECTS.glob("*_rejects.csv")):
        df = read_csv(f)
        if df.empty:
            continue
        out = pd.DataFrame({"source_table": f.name.replace("_rejects.csv", ""),
                            "reject_reason": df["reject_reason"],
                            "record": df.drop(columns="reject_reason").apply(lambda r: r.dropna().to_json(), axis=1)})
        copy_df(eng, out, "dw.reject_log")
        n_rej += len(out)
    logs = [read_csv(f).assign(script=f.name.replace("_log.csv", "")) for f in sorted(LOGS.glob("*_log.csv"))]
    if logs:
        lg = pd.concat(logs)[["script", "table", "step", "rows_before", "rows_after", "changed_or_flagged", "note"]]
        copy_df(eng, lg, "dw.cleaning_log")
    log_step(eng, "load_staging", "rejects + cleaning log", read=n_rej, rejected=n_rej, loaded=n_rej)


def build_dimensions(eng):
    run_sql_file(eng, "02_build_dimensions.sql")
    for t in ("dim_date", "dim_customer", "dim_product", "dim_seller", "dim_location"):
        n = scalar(eng, f"SELECT count(*) FROM dw.{t}")
        log_step(eng, "build_dimensions", t, loaded=n)


def build_fact(eng):
    run_sql_file(eng, "03_build_fact.sql")
    run_sql_file(eng, "04_enrich_customers.sql")
    n = scalar(eng, "SELECT count(*) FROM dw.fact_sales")
    src = scalar(eng, "SELECT count(*) FROM staging.stg_order_items")
    log_step(eng, "build_fact", "fact_sales", read=src, rejected=src - n, loaded=n)


def quality_checks(eng):
    """Stops the pipeline (raises an error) if something important is wrong."""
    problems = []
    src = scalar(eng, "SELECT count(*) FROM staging.stg_order_items")
    fact = scalar(eng, "SELECT count(*) FROM dw.fact_sales")
    if fact != src:
        problems.append(f"fact rows {fact} != order_items {src}")
    if scalar(eng, "SELECT count(*) - count(DISTINCT order_item_id) FROM dw.fact_sales") != 0:
        problems.append("grain violated: duplicate order_item_id in fact_sales")
    src_net = scalar(eng, "SELECT sum(gross_amount::numeric - discount_amount::numeric) FROM staging.stg_order_items")
    fact_net = scalar(eng, "SELECT sum(net_amount) FROM dw.fact_sales")
    if abs(src_net - fact_net) > 1:
        problems.append(f"net amount differs: staging {src_net} vs fact {fact_net}")
    fee_src = scalar(eng, "SELECT sum(shipping_fee::numeric) FROM staging.stg_shipping WHERE order_id IN "
                          "(SELECT order_id FROM staging.stg_order_items)")
    fee_fact = scalar(eng, "SELECT sum(shipping_fee) FROM dw.fact_sales")
    if abs(fee_src - fee_fact) > fee_src * 0.001:
        problems.append(f"shipping fee double counted or lost: shipments {fee_src} vs fact {fee_fact}")
    for col in ("customer_key", "product_key", "seller_key", "location_key"):
        share = scalar(eng, f"SELECT avg((({col}) = -1)::int) FROM dw.fact_sales")
        print(f"  share of lines pointing to the Unknown member for {col}: {share:.2%}")
        if share > 0.05:
            problems.append(f"too many Unknown {col}: {share:.1%}")
    nulls = scalar(eng, "SELECT count(*) FROM dw.fact_sales WHERE net_amount IS NULL OR quantity IS NULL")
    if nulls:
        problems.append(f"{nulls} fact rows with missing quantity or amount")
    log_step(eng, "quality_checks", "FAILED: " + "; ".join(problems) if problems else "all checks passed",
             read=src, rejected=src - fact, loaded=fact)
    if problems:
        raise RuntimeError("Quality checks failed: " + "; ".join(problems))


def main():
    eng = create_database()
    load_staging(eng)
    build_dimensions(eng)
    build_fact(eng)
    quality_checks(eng)
    print("\nWarehouse is ready: schema 'dw' in database", config()["name"])


if __name__ == "__main__":
    main()
