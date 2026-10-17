"""Step 3: clean payments, shipping and returns."""
import numpy as np
import pandas as pd

from common import (CLEAN, DATE_MAX, DATE_MIN, Log, clean_text, parse_dates,
                    parse_money, read_raw, save_clean, save_rejects)

log = Log("03_clean_payments_shipping_returns")


def money(series):
    """parse_money, forced to decimal numbers so later fixes (e.g. capped refunds) can be stored."""
    return parse_money(series).astype("float64")


orders = pd.read_csv(CLEAN / "orders.csv", dtype={"order_id": str}, parse_dates=["order_date"])
items = pd.read_csv(CLEAN / "order_items.csv", dtype=str)
for c in ("gross_amount", "discount_amount"):
    items[c] = pd.to_numeric(items[c])
items["net_amount"] = items["gross_amount"] - items["discount_amount"]
order_date = orders.set_index("order_id")["order_date"]

# ----------------------------------------------------------------- PAYMENTS
pay = read_raw("payments")
n0 = len(pay)
pay = pay.drop_duplicates()
log.add("payments", "exact duplicate rows removed", n0, len(pay))


def std_method(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower()
    # check the specific names first: "JazzCash" contains the word "cash", so COD must come last
    if "jazz" in t:
        return "JazzCash"
    if "easy" in t:
        return "EasyPaisa"
    if "bank" in t:
        return "Bank Transfer"
    if "card" in t:
        return "Card"
    if "cod" in t or "cash on" in t:
        return "COD"
    return "Unknown"


def std_pay_status(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower()
    if t in ("paid", "complete", "success"):
        return "Paid"
    if t.startswith("cancel"):
        return "Cancelled"
    if t.startswith("refund"):
        return "Refunded"
    if t == "pending":
        return "Pending"
    return "Unknown"


pay["payment_method"] = pay["payment_method"].map(std_method)
pay["payment_status"] = pay["payment_status"].map(std_pay_status)
log.count("payments", "payment_method (22 spellings) and payment_status standardised", len(pay), "-> 5 methods, 4 statuses")
pay["amount"] = money(pay["amount"])
neg = pay["amount"] < 0
pay.loc[neg, "amount"] = pay.loc[neg, "amount"].abs()
log.count("payments", "negative amount -> absolute value", neg.sum())
big = pay["amount"] > 1_000_000
pay.loc[big, "amount"] = np.nan
log.count("payments", "amount 9,999,999 placeholder set to blank", big.sum(), "(outliers)")
pay["payment_date"] = parse_dates(pay["payment_date"])
log.count("payments", "payment_date missing/impossible left blank", pay["payment_date"].isna().sum())
save_clean(pay, "payments")
# Some orders were paid in 2 parts (e.g. card + COD). The main method = the largest part.
orphan = ~pay["order_id"].isin(orders["order_id"])
save_rejects(pay[orphan].assign(reject_reason="order_id not in orders"), "payments")
log.add("payments", "payments for unknown orders -> rejects file", len(pay), len(pay) - orphan.sum())
main = (pay[~orphan].sort_values(["order_id", "amount"], ascending=[True, False], na_position="last")
        .groupby("order_id").agg(payment_method=("payment_method", "first"),
                                 payment_status=("payment_status", "first"),
                                 payment_parts=("payment_id", "size")).reset_index())
log.count("payments", "orders paid in more than one part (main method = largest part)", (main["payment_parts"] > 1).sum())
save_clean(main, "order_payment")

# ----------------------------------------------------------------- SHIPPING
sh = read_raw("shipping")
n0 = len(sh)
sh = sh.drop_duplicates()
log.add("shipping", "exact duplicate rows removed", n0, len(sh))


def std_courier(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower().replace(" ", "").replace(".", "")
    for key, name in (("tcs", "TCS"), ("leopard", "Leopards"), ("m&p", "M&P"), ("mnp", "M&P"),
                      ("trax", "Trax"), ("postex", "PostEx"), ("blue", "BlueEx")):
        if key in t:
            return name
    return "Unknown"


def std_ship_status(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower().replace("-", " ")
    if t in ("in transit", "shipped"):
        return "In Transit"
    if t.startswith("return") or t == "rto":
        return "Returned to Seller"
    if t in ("delivered", "deliverd", "complete", "completed"):
        return "Delivered"
    return "Unknown"


sh["courier"] = sh["courier"].map(std_courier)
sh["delivery_status"] = sh["delivery_status"].map(std_ship_status)
log.count("shipping", "courier (22 spellings) and delivery_status standardised", len(sh), "-> 6 couriers")
sh["shipped_date"] = parse_dates(sh["shipped_date"])
sh["delivered_date"] = parse_dates(sh["delivered_date"])
# a missing shipped date = order date + 1 day (couriers ship ~1 day after the order)
fix = sh["shipped_date"].isna()
sh.loc[fix, "shipped_date"] = sh["order_id"].map(order_date)[fix] + pd.Timedelta(days=1)
log.count("shipping", "missing/impossible shipped_date = order_date + 1 day", fix.sum())
# delivered before shipped (always exactly 2 days earlier): treat as swapped entry
swap = sh["delivered_date"] < sh["shipped_date"]
sh.loc[swap, ["shipped_date", "delivered_date"]] = sh.loc[swap, ["delivered_date", "shipped_date"]].values
log.count("shipping", "delivered_date before shipped_date -> dates swapped", swap.sum())
days = (sh["delivered_date"] - sh["shipped_date"]).dt.days
slow = days > 30
sh.loc[slow, "delivered_date"] = pd.NaT
log.count("shipping", "delivery time over 30 days (implausible) -> delivered_date blanked", slow.sum(), "(outliers)")
sh["shipping_days"] = (sh["delivered_date"] - sh["shipped_date"]).dt.days
sh["shipping_fee"] = money(sh["shipping_fee"])
neg = sh["shipping_fee"] < 0
sh.loc[neg, "shipping_fee"] = sh.loc[neg, "shipping_fee"].abs()
bad = sh["shipping_fee"].isna() | (sh["shipping_fee"] <= 0) | (sh["shipping_fee"] > 10_000)
sh["fee_imputed"] = bad
median_fee = sh.loc[~bad].groupby("courier")["shipping_fee"].median()
sh.loc[bad, "shipping_fee"] = sh.loc[bad, "courier"].map(median_fee).fillna(sh.loc[~bad, "shipping_fee"].median())
log.count("shipping", "fee blank/0/over 10,000 -> that courier's median fee (assumption)", bad.sum(), "(negatives made positive first)")
orphan = ~sh["order_id"].isin(orders["order_id"])
save_rejects(sh[orphan].assign(reject_reason="order_id not in orders"), "shipping")
log.add("shipping", "shipments for unknown orders -> rejects file", len(sh), len(sh) - orphan.sum())
save_clean(sh[~orphan], "shipping")

# ------------------------------------------------------------------ RETURNS
rt = read_raw("returns")
n0 = len(rt)
rt = rt.drop_duplicates()
log.add("returns", "exact duplicate rows removed", n0, len(rt))


def std_return_status(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower()
    if t in ("rejected", "declined"):
        return "Rejected"
    if t.startswith("refund"):
        return "Refunded"
    if t == "approved":
        return "Approved"
    return "Unknown"


REASONS = [("size", "Size/fit issue"), ("fit", "Size/fit issue"), ("describ", "Not as described"),
           ("picture", "Not as described"), ("qual", "Quality issue"), ("damag", "Damaged product"),
           ("mind", "Changed mind"), ("no longer", "Changed mind"), ("wrong item", "Wrong item delivered"),
           ("wrong product", "Wrong item delivered"), ("colo", "Colour mismatch"), ("late", "Late delivery"),
           ("missing", "Missing parts"), ("incomplete", "Missing parts")]


def std_reason(x):
    if pd.isna(x):
        return "Unknown"
    t = x.strip().lower()
    for key, name in REASONS:
        if key in t:
            return name
    return "Other"


rt["return_status"] = rt["return_status"].map(std_return_status)
rt["return_reason"] = rt["return_reason"].map(std_reason)
log.count("returns", "return_status standardised (Declined = Rejected)", len(rt), "-> Refunded / Approved / Rejected")
log.count("returns", "return_reason standardised (33 spellings -> 9 reasons + Unknown)", len(rt))
rt["is_valid_return"] = rt["return_status"] != "Rejected"
log.count("returns", "Rejected returns marked not valid (kept in table, ignored in metrics)", (~rt["is_valid_return"]).sum())

# returns must point to an existing order line
orphan = ~rt["order_item_id"].isin(items["order_item_id"])
save_rejects(rt[orphan].assign(reject_reason="order_item_id not in order_items"), "returns")
log.add("returns", "returns for unknown order lines -> rejects file", len(rt), len(rt) - orphan.sum())
rt = rt[~orphan].copy()
line = items.set_index("order_item_id")
rt["line_net"] = rt["order_item_id"].map(line["net_amount"])
rt["order_date"] = rt["order_item_id"].map(line["order_id"]).map(order_date)

# return date: median is 10 days after the order; negative or unreadable dates are repaired with that
rt["return_date"] = parse_dates(rt["return_date"])
lag = (rt["return_date"] - rt["order_date"]).dt.days
bad = rt["return_date"].isna() | (lag < 0) | (lag > 90)
median_lag = int(lag[~bad].median())
rt.loc[bad, "return_date"] = rt.loc[bad, "order_date"] + pd.Timedelta(days=median_lag)
log.count("returns", f"return_date impossible/before order -> order_date + {median_lag} days (median)", bad.sum())

# refund amount can never be higher than what the customer paid for the line
rt["refund_amount"] = money(rt["refund_amount"])
over = rt["refund_amount"] > rt["line_net"] * 1.05
rt.loc[over, "refund_amount"] = rt.loc[over, "line_net"].round(2)
log.count("returns", "refund_amount above the line's paid value -> capped at line value", over.sum(), "(outliers)")
zero = rt["is_valid_return"] & ~(rt["refund_amount"] > 0)
rt.loc[zero, "refund_amount"] = rt.loc[zero, "line_net"].round(2)
log.count("returns", "valid return with refund 0/blank -> full line value (assumption)", zero.sum())
rt.loc[~rt["is_valid_return"], "refund_amount"] = 0
save_clean(rt.drop(columns=["line_net", "order_date"]), "returns")
log.save()
print("\nDone. payments, order_payment, shipping and returns saved in data/clean")
