"""Shared helpers used by every cleaning script."""
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

# Folder layout. Can be overridden with an environment variable (needed later for Airflow/Docker).
BASE = Path(os.environ.get("NEXUS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
RAW = BASE / "raw"
CLEAN = BASE / "clean"
REJECTS = BASE / "rejects"
LOGS = BASE / "logs"
for p in (CLEAN, REJECTS, LOGS):
    p.mkdir(parents=True, exist_ok=True)

DATE_MIN = pd.Timestamp("2024-01-01")
DATE_MAX = pd.Timestamp("2025-06-30")


def read_raw(name):
    """Read a raw CSV as plain text so nothing is changed by accident."""
    return pd.read_csv(RAW / f"{name}.csv", dtype=str)


class Log:
    """Collects before/after counts for the data quality report."""

    def __init__(self, script):
        self.script, self.rows = script, []

    def add(self, table, step, before, after, note=""):
        self.rows.append({"table": table, "step": step, "rows_before": before,
                          "rows_after": after, "changed_or_flagged": before - after if before != after else "",
                          "note": note})
        print(f"[{table}] {step}: {before} -> {after} {note}")

    def count(self, table, step, n, note=""):
        """For fixes that change values, not row counts."""
        self.rows.append({"table": table, "step": step, "rows_before": "", "rows_after": "",
                          "changed_or_flagged": int(n), "note": note})
        print(f"[{table}] {step}: {int(n)} values {note}")

    def save(self):
        pd.DataFrame(self.rows).to_csv(LOGS / f"{self.script}_log.csv", index=False)


def clean_text(s):
    """Trim spaces and collapse repeated spaces; blanks become missing."""
    s = s.astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
    return s.replace("", pd.NA)


def parse_dates(s):
    """Understands 2024-05-01, 'May 24 2025' and 05/03/2024 (day/month/year)."""
    s = s.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in ("%Y-%m-%d", "%b %d %Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        todo = out.isna() & s.notna()
        if not todo.any():
            break
        out[todo] = pd.to_datetime(s[todo], format=fmt, errors="coerce")
    return out


def parse_money(s):
    """'Rs. 2,290' -> 2290.0 ; bad text -> NaN."""
    s = s.astype("string").str.replace(r"(?i)rs\.?|pkr|,", "", regex=True).str.strip()
    return pd.to_numeric(s, errors="coerce")


def save_clean(df, name):
    df.to_csv(CLEAN / f"{name}.csv", index=False)


def save_rejects(df, name):
    df.to_csv(REJECTS / f"{name}_rejects.csv", index=False)
