"""Step 2: clean orders and order_items (the heart of FactSales)."""
import numpy as np
import pandas as pd

from common import (CLEAN, DATE_MAX, DATE_MIN, Log, clean_text, parse_dates,
                    parse_money, read_raw, save_clean, save_rejects)

log = Log("02_clean_orders")
locations = pd.read_csv(CLEAN / "locations.csv", dtype=str)
customers = pd.read_csv(CLEAN / "customers.csv", dtype=str)
cust_map = pd.read_csv(CLEAN / "customer_id_map.csv", dtype=str).set_index("customer_id")["master_id"]
products = pd.read_csv(CLEAN / "products.csv", dtype=str)
sellers = pd.read_csv(CLEAN / "sellers.csv", dtype=str)
products["list_price"] = pd.to_numeric(products["list_price"])

# ------------------------------------------------------------------- ORDERS
o = read_raw("orders")
n0 = len(o)
o = o.drop_duplicates()
log.add("orders", "exact duplicate rows removed", n0, len(o))

# --- status and channel: many spellings -> a few standard values
def std_status(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower().replace("-", " ")
    if t.startswith("cancel"):
        return "Cancelled"
    if t.startswith("return"):
        return "Returned"
    if t in ("in transit", "shipped"):
        return "In Transit"
    if t in ("delivered", "deliverd", "complete", "completed"):
        return "Delivered"
    return "Unknown"


def std_channel(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower()
    if t in ("mobile web", "mweb"):
        return "Mobile Web"
    if t in ("app", "mobile app"):
        return "App"
    if t in ("web", "website"):
        return "Web"
    return "Unknown"


o["order_status"] = o["order_status"].map(std_status)
o["channel"] = o["channel"].map(std_channel)
log.count("orders", "order_status standardised", len(o), "-> Delivered / Cancelled / Returned / In Transit")
log.count("orders", "channel standardised", len(o), "-> App / Web / Mobile Web")

# --- customers: duplicates point to their master; unknown customers -> C_UNKNOWN
o["customer_id"] = o["customer_id"].map(cust_map).fillna(o["customer_id"])
orphan = ~o["customer_id"].isin(customers["customer_id"])
log.count("orders", "customer_id not found -> 'C_UNKNOWN' (broken reference)", orphan.sum())
o.loc[orphan, "customer_id"] = "C_UNKNOWN"

# --- order_date: repair impossible dates from other data
o["order_date"] = parse_dates(o["order_date"])
ship = read_raw("shipping")
ship["shipped_date"] = parse_dates(ship["shipped_date"])
ship = ship.drop_duplicates("order_id").set_index("order_id")["shipped_date"]
bad = o["order_date"].isna() | (o["order_date"] < DATE_MIN) | (o["order_date"] > DATE_MAX)
log.count("orders", "impossible/unreadable order_date found", bad.sum(), "(e.g. 2024-13-05, 00/00/2024, years 2026-2027)")
# Method 1: couriers ship about 1 day after the order (checked on good rows)
fix1 = o["order_id"].map(ship) - pd.Timedelta(days=1)
use1 = bad & fix1.between(DATE_MIN, DATE_MAX)
o.loc[use1, "order_date"] = fix1[use1]
log.count("orders", "order_date repaired = shipped_date - 1 day", use1.sum())
# Method 2: order numbers grow with time, so copy the date of the previous good order
bad2 = bad & ~use1
o["_n"] = o["order_id"].str[1:].astype(int)
o = o.sort_values("_n")
good_dates = o["order_date"].where(~bad2)
o.loc[bad2, "order_date"] = good_dates.ffill().bfill()[bad2]
log.count("orders", "order_date repaired from neighbouring order numbers", bad2.sum())
o = o.drop(columns="_n")

# --- ship city: if missing, assume the customer's own city (89% of orders ship to it)
cust_city = customers.set_index("customer_id")["city_id"]
valid_city = o["ship_city_id"].isin(locations["city_id"])
o["ship_city_imputed"] = False
fill = (~valid_city) & o["customer_id"].map(cust_city).isin(locations["city_id"]) & (o["customer_id"].map(cust_city) != "-1")
o.loc[fill, "ship_city_id"] = o["customer_id"].map(cust_city)[fill]
o.loc[fill, "ship_city_imputed"] = True
log.count("orders", "missing ship city filled with customer's city (assumption)", fill.sum())
rest = ~o["ship_city_id"].isin(locations["city_id"])
log.count("orders", "ship city still unknown -> 'Unknown' (-1)", rest.sum())
o.loc[rest, "ship_city_id"] = "-1"

# --- discount code -> percentage ('Save15', ' SAVE15' -> 0.15)
o["discount_pct"] = pd.to_numeric(o["discount_code"].str.upper().str.extract(r"(\d+)")[0], errors="coerce") / 100
o["discount_pct"] = o["discount_pct"].fillna(0.0)
o["order_total"] = parse_money(o["order_total"])

# ------------------------------------------------------------- ORDER ITEMS
oi = read_raw("order_items")
n0 = len(oi)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
q = oi["quantity"].str.strip().str.lower()
oi["quantity"] = pd.to_numeric(q.replace(WORDS), errors="coerce")
log.count("order_items", "quantity words/decimals converted to whole numbers (one, 2.0 ...)", q.isin(list(WORDS)).sum() + q.str.contains(r"\.", na=False).sum())
neg = oi["quantity"] < 0
log.count("order_items", "negative quantity -> absolute value (sign error)", neg.sum())
oi.loc[neg, "quantity"] = oi.loc[neg, "quantity"].abs()
oi["unit_price"] = parse_money(oi["unit_price"])
oi["discount_amount"] = pd.to_numeric(oi["discount_amount"], errors="coerce")

# --- broken references
oi["seller_id"] = oi["seller_id"].where(oi["seller_id"].isin(sellers["seller_id"]), "S_UNKNOWN")
orphan = ~oi["product_id"].isin(products["product_id"])
log.count("order_items", "product_id not found -> 'P_UNKNOWN' (broken reference)", orphan.sum())
oi.loc[orphan, "product_id"] = "P_UNKNOWN"
orphan = ~oi["order_id"].isin(o["order_id"])
rej = oi[orphan].assign(reject_reason="order_id not in orders")
save_rejects(rej, "order_items")
log.add("order_items", "lines whose order does not exist -> rejects file", len(oi), len(oi) - orphan.sum())
oi = oi[~orphan].copy()

# --- unit price: negative = sign error; 0 or 9,999,999 = placeholder -> use product list price
LIST_RATIO = 1.026  # median of unit_price / list_price on good rows (price drifts ~2.6% above list)
lp = oi["product_id"].map(products.set_index("product_id")["list_price"])
neg = oi["unit_price"] < 0
oi.loc[neg, "unit_price"] = oi.loc[neg, "unit_price"].abs()
log.count("order_items", "negative unit_price -> absolute value", neg.sum())
badp = oi["unit_price"].isna() | (oi["unit_price"] <= 0) | (oi["unit_price"] > 500_000)
# real prices sit within 0.5x-2x of the list price (checked); anything else is a decimal/scale error
badp |= lp.notna() & ~(oi["unit_price"] / lp).between(0.5, 2)
fixp = badp & lp.notna()
oi.loc[fixp, "unit_price"] = (lp[fixp] * LIST_RATIO).round(0)
log.count("order_items", "unit_price 0/9999999/blank/scale error (outside 0.5x-2x list price) -> list_price x 1.026 (assumption)", fixp.sum())
oi["price_repaired"] = fixp | neg

# --- quantity outliers (500, 0): rebuild from the order total when the order has one line
badq = oi["quantity"].isna() | (oi["quantity"] <= 0) | (oi["quantity"] > 10)
lines = oi.groupby("order_id")["order_id"].transform("size")
tot = oi["order_id"].map(o.set_index("order_id")["order_total"])
pct = oi["order_id"].map(o.set_index("order_id")["discount_pct"])
est = (tot / (oi["unit_price"] * (1 - pct))).round()
use = badq & (lines == 1) & tot.between(1, 500_000) & est.between(1, 10)
oi.loc[use, "quantity"] = est[use]
rest = badq & ~use
oi.loc[rest, "quantity"] = 1
oi["quantity_repaired"] = badq | neg
log.count("order_items", "quantity outliers (500, 0, blank) rebuilt from order_total (single-line orders)", use.sum())
log.count("order_items", "quantity outliers in multi-line orders set to 1 (most common value)", rest.sum(), "(assumption)")

# --- discount: blank or impossible -> order-level percentage x gross
gross = oi["quantity"] * oi["unit_price"]
expected = (gross * oi["order_id"].map(o.set_index("order_id")["discount_pct"])).round(2)
bad_d = oi["discount_amount"].isna() | (oi["discount_amount"] < 0) | (oi["discount_amount"] > gross)
log.count("order_items", "blank/impossible discount_amount -> gross x discount % of the order", bad_d.sum())
oi.loc[bad_d, "discount_amount"] = expected[bad_d]
oi["gross_amount"] = gross.round(2)

# --- order_total outliers: rebuild from the lines
net = (oi["gross_amount"] - oi["discount_amount"]).groupby(oi["order_id"]).sum()
bad_t = o["order_total"].isna() | (o["order_total"] <= 0) | (o["order_total"] > 1_000_000)
o.loc[bad_t, "order_total"] = o.loc[bad_t, "order_id"].map(net).round(0)
log.count("orders", "order_total negative/9999999/blank -> rebuilt from its lines", bad_t.sum())

o["is_cancelled"] = o["order_status"] == "Cancelled"
save_clean(o, "orders")
save_clean(oi, "order_items")
log.save()
print(f"\nDone. orders={len(o)}  order_items={len(oi)}")
