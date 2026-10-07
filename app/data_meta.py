"""Year-specific facts read from the plan database itself (2026-10-06).

A plan-year database carries a small `meta` table (key/value), written by build_2027_db.py and
future yearly builders:
    data_year         2027                 the CMS plan year the data describes
    oop_cap           2400                 Part D yearly out-of-pocket cap (from the Landscape file)
    data_vintage      "CMS Oct 2026 ..."   the "Data:" label on reports and in the plan picker
    prices_estimated  1/0                  1 = drug prices are carried from the prior year's file
    prices_vintage    "CMS Q2 2026"        where the prices came from
and a `negotiated_prices` table (drug name -> Medicare negotiated monthly price for that year).

Moving to a new plan year = loading a new database. A database without `meta` (the original
2026 file) behaves exactly as before: the defaults below.
"""
import os
import sqlite3
from functools import lru_cache

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn.db")

DEFAULTS = {"data_year": "2026", "data_vintage": "CMS Q1 2026", "oop_cap": "", "prices_estimated": "0",
            "prices_vintage": ""}


def _stamp(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


@lru_cache(maxsize=8)
def _read(path, _mtime):
    out = {"meta": None, "mfp": None}
    if not os.path.exists(path):
        return out
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" in tables:
            out["meta"] = dict(conn.execute("SELECT key, value FROM meta"))
        if "negotiated_prices" in tables:
            out["mfp"] = {k.lower().strip(): float(v) for k, v in conn.execute("SELECT drug, price FROM negotiated_prices")}
    finally:
        conn.close()
    return out


def _data(path=None):
    path = path or DB_PATH
    return _read(path, _stamp(path))


def has_meta(path=None):
    return _data(path)["meta"] is not None


def meta(path=None):
    m = dict(DEFAULTS)
    m.update(_data(path)["meta"] or {})
    return m


def data_year(path=None):
    return int(meta(path)["data_year"])


def oop_cap_override(path=None):
    v = (meta(path).get("oop_cap") or "").strip()
    return float(v) if v else None


def data_vintage(path=None):
    return meta(path)["data_vintage"]


def prices_estimated(path=None):
    return meta(path).get("prices_estimated") == "1"


def negotiated_prices(path=None):
    """{drug name: price} from the database, or None when the database has no such table."""
    return _data(path)["mfp"]
