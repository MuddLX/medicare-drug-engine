"""Carriers the agency doesn't sell (Quartz, Align) are left out of every plan list (2026-10-27)."""
import os
import sqlite3
import pytest
import app.main as M

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn_2027.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="needs medicare_mn_2027.db")


def _contracts(county, monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv("HIDDEN_CONTRACTS", raising=False)
    else:
        monkeypatch.setenv("HIDDEN_CONTRACTS", value)
    ma, pd = M.eligible_plan_rows(sqlite3.connect(DB), county)
    return {r[0] for r in ma + pd}


def test_quartz_and_align_hidden_by_default(monkeypatch):
    county = sqlite3.connect(DB).execute("SELECT county_name FROM service_area WHERE contract_id='H9834' LIMIT 1").fetchone()[0]
    seen = _contracts(county, monkeypatch)
    assert "H9834" not in seen and "H3186" not in seen and seen      # other carriers still listed


def test_setting_can_show_them_again(monkeypatch):
    county = sqlite3.connect(DB).execute("SELECT county_name FROM service_area WHERE contract_id='H9834' LIMIT 1").fetchone()[0]
    assert "H9834" in _contracts(county, monkeypatch, "none")
