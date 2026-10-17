"""Step 1: clean the four 'lookup' tables: locations, sellers, products, customers."""
import re

import numpy as np
import pandas as pd

from common import (Log, clean_text, parse_dates, parse_money, read_raw,
                    save_clean, save_rejects)

log = Log("01_clean_lookups")

# ---------------------------------------------------------------- LOCATIONS
loc = read_raw("locations")
n = len(loc)
loc["city"] = clean_text(loc["city"])
PROVINCES = {
    "punjab": "Punjab", "panjab": "Punjab",
    "sindh": "Sindh", "sind": "Sindh",
    "khyber pakhtunkhwa": "Khyber Pakhtunkhwa", "kp": "Khyber Pakhtunkhwa", "kpk": "Khyber Pakhtunkhwa",
    "balochistan": "Balochistan", "baluchistan": "Balochistan",
    "gilgit-baltistan": "Gilgit-Baltistan", "gilgit baltistan": "Gilgit-Baltistan", "gb": "Gilgit-Baltistan",
    "azad kashmir": "Azad Kashmir", "azad jammu & kashmir": "Azad Kashmir",
    "islamabad": "Islamabad Capital Territory",
}
before_bad = loc["province"].notna().sum()
loc["province"] = clean_text(loc["province"]).str.lower().map(PROVINCES)
log.count("locations", "province spellings standardised", before_bad - loc["province"].isna().sum(), "-> standard province names")
log.count("locations", "missing province set to 'Unknown'", loc["province"].isna().sum(), "(cannot be repaired from other data)")
loc["province"] = loc["province"].fillna("Unknown")
for c in ("latitude", "longitude"):
    loc[c] = pd.to_numeric(loc[c], errors="coerce")
bad = (~loc["latitude"].between(23, 37)) & loc["latitude"].notna() | (~loc["longitude"].between(60, 78)) & loc["longitude"].notna()
log.count("locations", "coordinates outside Pakistan set to blank", bad.sum(), "(outliers)")
loc.loc[bad, ["latitude", "longitude"]] = np.nan
loc["region"] = clean_text(loc["region"]).str.title()
loc["population_tier"] = clean_text(loc["population_tier"])
unknown = pd.DataFrame([{"city_id": "-1", "city": "Unknown", "province": "Unknown", "region": "Unknown",
                         "latitude": np.nan, "longitude": np.nan, "population_tier": "Unknown"}])
loc = pd.concat([unknown, loc], ignore_index=True)
log.add("locations", "added 'Unknown' member (-1)", n, len(loc))
save_clean(loc, "locations")

# ------------------------------------------------------------------ SELLERS
sel = read_raw("sellers")
n = len(sel)
sel["seller_name"] = clean_text(sel["seller_name"])
sel["seller_tier"] = clean_text(sel["seller_tier"]).str.title()
log.count("sellers", "missing tier set to 'Unknown'", sel["seller_tier"].isna().sum())
sel["seller_tier"] = sel["seller_tier"].fillna("Unknown")
sel["join_date"] = parse_dates(sel["join_date"])
sel["rating"] = pd.to_numeric(sel["rating"], errors="coerce")
bad = (sel["rating"] < 0) | (sel["rating"] > 5)
log.count("sellers", "ratings outside 0-5 set to blank", bad.sum(), "(outliers)")
sel.loc[bad, "rating"] = np.nan
sel["city_id"] = sel["city_id"].where(sel["city_id"].isin(loc["city_id"]), "-1")
unknown = pd.DataFrame([{"seller_id": "S_UNKNOWN", "seller_name": "Unknown", "join_date": pd.NaT,
                         "seller_tier": "Unknown", "city_id": "-1", "rating": np.nan}])
sel = pd.concat([unknown, sel], ignore_index=True)
log.add("sellers", "added 'Unknown' member", n, len(sel))
save_clean(sel, "sellers")

# ----------------------------------------------------------------- PRODUCTS
prod = read_raw("products")
n = len(prod)
CATEGORY_RULES = [  # (keyword found in the lower-case text, standard name)
    ("mobile", "Mobile & Accessories"), ("elec", "Electronics"),
    ("fashion", "Fashion"), ("cloth", "Fashion"),
    ("home", "Home & Kitchen"), ("beauty", "Beauty & Personal Care"), ("personal care", "Beauty & Personal Care"),
    ("footwear", "Footwear"), ("shoe", "Footwear"),
    ("sport", "Sports & Outdoors"), ("grocer", "Grocery"), ("food", "Grocery"),
    ("health", "Health & Wellness"), ("toy", "Toys & Games"),
    ("book", "Books & Stationery"), ("station", "Books & Stationery"),
    ("auto", "Automotive"), ("car acc", "Automotive"),
]


def std_category(x):
    if pd.isna(x):
        return "Unknown"
    t = x.lower()
    for key, name in CATEGORY_RULES:
        if key in t:
            return name
    return "Unknown"


prod["category"] = clean_text(prod["category"]).map(std_category)
log.count("products", "category names standardised", len(prod), f"-> {prod['category'].nunique()} categories")
prod["product_name"] = clean_text(prod["product_name"])
prod["brand"] = clean_text(prod["brand"])
log.count("products", "missing brand set to 'Unknown'", prod["brand"].isna().sum())
prod["brand"] = prod["brand"].fillna("Unknown")
prod["sub_category"] = clean_text(prod["sub_category"])
log.count("products", "missing sub_category set to 'Unknown'", prod["sub_category"].isna().sum())
prod["sub_category"] = prod["sub_category"].fillna("Unknown")
for c in ("list_price", "cost_price", "weight_kg"):
    prod[c] = parse_money(prod[c])
bad = (prod["list_price"] <= 0) | (prod["list_price"] > 1_000_000)
log.count("products", "impossible list_price (<=0 or >1,000,000) set to blank", bad.sum(), "(outliers)")
prod.loc[bad, "list_price"] = np.nan
bad = (prod["cost_price"] <= 0)
prod.loc[bad, "cost_price"] = np.nan
bad = (prod["weight_kg"] <= 0) | (prod["weight_kg"] > 100)
log.count("products", "impossible weight (<=0 or >100 kg) set to blank", bad.sum(), "(outliers)")
prod.loc[bad, "weight_kg"] = np.nan
# A missing price can be repaired from the other price using the typical cost/list ratio of the category
ratio = (prod["cost_price"] / prod["list_price"]).groupby(prod["category"]).transform("median")
fix = prod["cost_price"].isna() & prod["list_price"].notna()
log.count("products", "missing cost_price estimated = list_price x category median ratio", fix.sum(), "(assumption)")
prod.loc[fix, "cost_price"] = (prod["list_price"] * ratio)[fix].round(0)
fix = prod["list_price"].isna() & prod["cost_price"].notna()
log.count("products", "missing list_price estimated = cost_price / category median ratio", fix.sum(), "(assumption)")
prod.loc[fix, "list_price"] = (prod["cost_price"] / ratio)[fix].round(0)
prod["cost_above_list_flag"] = prod["cost_price"] > prod["list_price"]
log.count("products", "flagged cost_price > list_price (kept, flag column)", prod["cost_above_list_flag"].sum())
prod["seller_id"] = prod["seller_id"].where(prod["seller_id"].isin(sel["seller_id"]), "S_UNKNOWN")
unknown = pd.DataFrame([{"product_id": "P_UNKNOWN", "product_name": "Unknown", "category": "Unknown",
                         "sub_category": "Unknown", "brand": "Unknown", "list_price": np.nan,
                         "cost_price": np.nan, "weight_kg": np.nan, "seller_id": "S_UNKNOWN",
                         "cost_above_list_flag": False}])
prod = pd.concat([unknown, prod], ignore_index=True)
log.add("products", "added 'Unknown' member", n, len(prod))
save_clean(prod, "products")

# ---------------------------------------------------------------- CUSTOMERS
cus = read_raw("customers")
n = len(cus)
cus["full_name"] = clean_text(cus["full_name"]).str.title()
cus["email"] = clean_text(cus["email"]).str.lower()
GENDER = {"m": "Male", "male": "Male", "f": "Female", "female": "Female"}
cus["gender"] = clean_text(cus["gender"]).str.lower().map(GENDER).fillna("Unknown")


def std_phone(x):
    if pd.isna(x):
        return pd.NA
    d = re.sub(r"\D", "", x)
    if d.startswith("92") and len(d) == 12:
        d = "0" + d[2:]
    if len(d) == 10 and d.startswith("3"):
        d = "0" + d
    return d if (len(d) == 11 and d.startswith("03")) else pd.NA


had = cus["phone"].notna().sum()
cus["phone"] = cus["phone"].map(std_phone)
log.count("customers", "phones standardised to 03XXXXXXXXX", cus["phone"].notna().sum(), f"(of {had} given; the rest were invalid)")
cus["registration_date"] = parse_dates(cus["registration_date"])
cus["birth_year"] = pd.to_numeric(cus["birth_year"], errors="coerce")
bad = (cus["birth_year"] < 1930) | (cus["birth_year"] > 2010)
log.count("customers", "implausible birth_year (<1930 or >2010) set to blank", bad.sum(), "(outliers)")
cus.loc[bad, "birth_year"] = np.nan
log.count("customers", "missing/invalid city_id mapped to Unknown (-1)", (~cus["city_id"].isin(loc["city_id"])).sum())
cus["city_id"] = cus["city_id"].where(cus["city_id"].isin(loc["city_id"]), "-1")

# Duplicate customers: same e-mail AND same name (never name alone), or same phone AND same name.
cus["name_key"] = cus["full_name"].str.lower()
cus["master_id"] = cus["customer_id"]
for key in ("email", "phone"):
    ok = cus[key].notna()
    first = cus[ok].sort_values("customer_id").groupby([key, "name_key"])["customer_id"].transform("first")
    cus.loc[ok, "master_id"] = first
# follow chains so every duplicate points to its final master
for _ in range(3):
    cus["master_id"] = cus["master_id"].map(cus.set_index("customer_id")["master_id"]).fillna(cus["master_id"])
mapping = cus.loc[cus["master_id"] != cus["customer_id"], ["customer_id", "master_id"]].rename(
    columns={"customer_id": "duplicate_id"})
log.add("customers", "duplicate customers merged under one master ID", n, n - len(mapping),
        f"({len(mapping)} duplicate IDs saved in customer_id_map.csv)")
full_map = cus[["customer_id", "master_id"]]
save_clean(full_map, "customer_id_map")
masters = cus[cus["master_id"] == cus["customer_id"]].drop(columns=["name_key", "master_id"]).copy()
# keep the earliest registration date seen for each person
earliest = cus.groupby("master_id")["registration_date"].min()
masters["registration_date"] = masters["customer_id"].map(earliest)
unknown = pd.DataFrame([{"customer_id": "C_UNKNOWN", "full_name": "Unknown", "email": pd.NA, "phone": pd.NA,
                         "gender": "Unknown", "registration_date": pd.NaT, "city_id": "-1", "birth_year": np.nan}])
masters = pd.concat([unknown, masters], ignore_index=True)
save_clean(masters, "customers")
log.save()
print("\nDone. Clean files are in data/clean, counts in data/logs.")
