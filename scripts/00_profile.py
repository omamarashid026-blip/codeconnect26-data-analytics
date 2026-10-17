import pandas as pd
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"

for f in ["customers", "products", "sellers", "locations", "orders",
          "order_items", "payments", "shipping", "returns"]:
    df = pd.read_csv(RAW / f"{f}.csv", dtype=str)
    print(f"{f:12} rows={len(df):>7}  columns={len(df.columns)}  "
          f"exact_duplicates={df.duplicated().sum()}")