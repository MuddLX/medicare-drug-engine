"""The engine takes its year-specific facts from the database it is given (2026-10-06), so moving
to a new plan year means loading a new database, not editing code: data year, out-of-pocket cap,
the "Data: CMS ..." label, negotiated drug prices and plan labels all come from a `meta` table /
the plans table. A database without a meta table (today's 2026 file) keeps today's behaviour."""
import sqlite3
import pytest
import app.main as M
from app import data_meta as DM
from app import drug_year as DY


def _db(tmp_path, meta=None, mfp=None, plans=()):
    p = tmp_path / "m.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE plans (contract_id TEXT, plan_id TEXT, segment_id TEXT, carrier TEXT, carrier_name TEXT,"
              " plan_name TEXT, formulary_id TEXT, premium REAL, deductible REAL, plan_type TEXT, is_default INTEGER)")
    for row in plans:
        c.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,0)", row)
    if meta is not None:
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    if mfp is not None:
        c.execute("CREATE TABLE negotiated_prices (drug TEXT PRIMARY KEY, price REAL)")
        c.executemany("INSERT INTO negotiated_prices VALUES (?,?)", list(mfp.items()))
    c.commit(); c.close()
    return str(p)


@pytest.fixture
def use_db(monkeypatch):
    def _use(path):
        monkeypatch.setattr(DM, "DB_PATH", path)
        monkeypatch.setattr(M, "DB_PATH", path)
    return _use


def test_no_meta_table_keeps_todays_2026_behaviour(tmp_path, use_db):
    use_db(_db(tmp_path))
    assert DM.data_year() == 2026
    assert DY.oop_cap() == 2100.0
    assert M.data_vintage() == "CMS Q1 2026"
    assert M.get_mfp("eliquis") == 231.00


def test_meta_sets_year_cap_and_vintage(tmp_path, use_db):
    use_db(_db(tmp_path, meta={"data_year": "2027", "oop_cap": "2400", "data_vintage": "CMS Oct 2026 (2027 plans)"}))
    assert DM.data_year() == 2027
    assert DY.oop_cap() == 2400.0
    assert M.data_vintage() == "CMS Oct 2026 (2027 plans)"


def test_cap_falls_back_to_the_year_table_when_meta_has_no_cap(tmp_path, use_db):
    use_db(_db(tmp_path, meta={"data_year": "2027"}))
    assert DY.oop_cap() == 2400.0


def test_negotiated_prices_come_from_the_database(tmp_path, use_db):
    use_db(_db(tmp_path, meta={"data_year": "2027"}, mfp={"ozempic": 274.0, "eliquis": 231.0}))
    assert M.get_mfp("Ozempic") == 274.0
    assert M.get_mfp("stelara") is None          # only what the database lists


def test_plan_labels_come_from_the_database_when_it_has_meta(tmp_path, use_db):
    path = _db(tmp_path, meta={"data_year": "2027"},
               plans=[("H5959", "019", "000", "Blue Cross Essential ($0)", "", "Blue Cross Medicare Advantage Essential (PPO)",
                       "00027044", 0, 700, "Medicare Advantage")])
    use_db(path)
    conn = sqlite3.connect(path)
    assert M.plan_label(conn, "H5959", "019", "Blue Cross Medicare Advantage Essential (PPO)") == "Blue Cross Essential ($0)"
    assert M.plan_label(conn, "H9999", "001", "Some Long Plan Name (PPO)") == "Some Long Plan Name (PPO)"


def test_legacy_database_keeps_hand_written_labels(tmp_path, use_db):
    path = _db(tmp_path, plans=[("H4882", "009", "000", "HealthPartners Journey Pace", "", "x", "1", 0, 0, "MA")])
    use_db(path)
    conn = sqlite3.connect(path)
    assert M.plan_label(conn, "H3219", "008", "whatever") == "Aetna Signature Fit"


def test_month_names_do_not_depend_on_a_year():
    assert M.calendar_month(2) == "February"
