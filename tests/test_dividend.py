"""dividend.py 口径测试：当日除息、首次分红、外推日期已过"""

import pandas as pd
import pytest

from basis import db
from basis import dividend as D

P = "300"
S = 4000.0
PX = 10.0


def _ev(code, ex, dps, plan, report, impl=None):
    return {"product": P, "stock_code": code, "ex_date": ex, "div_per_share": dps,
            "plan_date": plan, "sgm_date": None, "impl_date": impl or ex, "report_date": report}


@pytest.fixture(scope="module")
def panel():
    conn = db.connect(":memory:")
    days = pd.bdate_range("2021-01-01", "2024-12-31").strftime("%Y-%m-%d")
    codes = ["A", "B", "C"]
    db.upsert(conn, "index_daily", pd.DataFrame({"product": P, "date": days, "close": S}))
    db.upsert(conn, "constituent_daily", pd.DataFrame(
        [{"product": P, "date": d, "stock_code": c, "close": PX} for d in days for c in codes]))
    db.upsert(conn, "index_weight", pd.DataFrame(
        [{"product": P, "date": "2024-05-31", "stock_code": c, "weight": 100 / 3} for c in codes]))
    ev = [
        # A：年报分红，每年 6 月中，预案→除息约 60 天；2024 年 6/14 除息、6/7 发实施公告
        _ev("A", "2021-06-15", 1.0, "2021-04-15", "2020-12-31"),
        _ev("A", "2022-06-15", 1.0, "2022-04-16", "2021-12-31"),
        _ev("A", "2023-06-15", 1.0, "2023-04-15", "2022-12-31"),
        _ev("A", "2024-06-14", 1.0, "2024-04-15", "2023-12-31", impl="2024-06-07"),
        # B：从没分过中期红，2024 年首次，8/20 预案、9/13 实施公告、9/20 除息
        _ev("B", "2024-09-20", 0.5, "2024-08-20", "2024-06-30", impl="2024-09-13"),
        # C：历史上预案后 30 天除息，2024 年拖到 7/31 才除息
        _ev("C", "2021-05-31", 1.0, "2021-05-01", "2020-12-31"),
        _ev("C", "2022-05-31", 1.0, "2022-05-01", "2021-12-31"),
        _ev("C", "2023-05-31", 1.0, "2023-05-01", "2022-12-31"),
        _ev("C", "2024-07-31", 1.0, "2024-04-01", "2023-12-31", impl="2024-07-25"),
    ]
    db.upsert(conn, "dividend_announcement", pd.DataFrame(ev))
    return D.Panel(conn, P)


def _rows(panel, asof, code):
    return [r for r in D.predict_stocks(panel, asof, detail=True) if r["stock_code"] == code]


def test_exdate_today_excluded(panel):
    # 前一天：已公布的 6/14 计入
    before = _rows(panel, "2024-06-13", "A")
    assert any(r["ex_date"] == pd.Timestamp("2024-06-14") and r["日期依据"] == "已公布" for r in before)
    # 当天：收盘价已除息，不再计入；窗口内也不应有任何 A 的分红
    assert not any(r["ex_date"] <= pd.Timestamp("2024-06-14") for r in _rows(panel, "2024-06-14", "A"))
    pts, det = D.forecast(panel, "2024-06-14", "2024-07-14")
    assert det.empty or "A" not in set(det["stock_code"])


def test_first_time_dividend_counted(panel):
    rows = _rows(panel, "2024-08-30", "B")
    assert len(rows) == 1
    r = rows[0]
    assert r["金额依据"] == "已公告" and r["dps"] == 0.5
    assert r["日期依据"] == "预案+市场间隔"
    assert r["ex_date"] > pd.Timestamp("2024-08-30")
    assert r["pts"] == pytest.approx(0.5 / PX * (100 / 3 / 100) * S)


def test_overdue_extrapolation_postponed(panel):
    # 预案 4/1 + 历史间隔 30 天 = 5/1，早已过去，实施公告还没发 → 顺延到 6/14 + 10 天
    rows = _rows(panel, "2024-06-14", "C")
    cur = [r for r in rows if r["ex_date"].year == 2024]
    assert len(cur) == 1
    assert cur[0]["ex_date"] == pd.Timestamp("2024-06-24")
    assert cur[0]["日期依据"].endswith("顺延")


def test_roll_forward_skips_today():
    assert D._roll_forward(pd.Timestamp("2023-06-14"), pd.Timestamp("2024-06-14")) == pd.Timestamp("2025-06-14")
    assert D._roll_forward(pd.Timestamp("2023-06-15"), pd.Timestamp("2024-06-14")) == pd.Timestamp("2024-06-15")


# ---------------------------------------------------------------- 全年合计 / 利润×支付率

def test_extrapolate_requires_consecutive_years():
    d = pd.Series([1.0, 1.1, 1.2], index=[2019, 2021, 2022])      # 2020 没分红
    assert D.extrapolate(d)[1] == "沿用上年"
    assert D.extrapolate(d, consecutive=False)[1] == "线性外推"
    assert D.extrapolate(pd.Series([1.0, 1.1, 1.2], index=[2020, 2021, 2022])) == (pytest.approx(1.3), "线性外推")


def test_np_forecast():
    fin = {(y, "09-30"): 75.0 for y in (2020, 2021, 2022)}
    fin.update({(y, "12-31"): 100.0 for y in (2020, 2021, 2022)})
    fin[(2023, "09-30")] = 90.0
    assert D.np_forecast(fin, 2023) == pytest.approx(120.0)      # 比例稳定 0.75 → 90/0.75
    fin[(2020, "12-31")] = 300.0                                  # 比例不稳定 → 三季报 + 上年Q4
    assert D.np_forecast(fin, 2023) == pytest.approx(90.0 + 25.0)
    fin[(2023, "12-31")] = 130.0                                  # 年报已出 → 实际值
    assert D.np_forecast(fin, 2023) == 130.0
    assert D.np_forecast({}, 2023) is None


def test_profit_scaled_total_caps_payout():
    totals = pd.Series([2.0], index=[2022])
    fin = {(2022, "12-31"): 100.0, (2023, "12-31"): 150.0}
    assert D.profit_scaled_total(totals, fin, 2023, {2022: 50.0}) == pytest.approx(3.0)
    # 去年支付率 200%（>100% 截断）→ 按 100% 算
    assert D.profit_scaled_total(totals, fin, 2023, {2022: 200.0}) == pytest.approx(1.5)


@pytest.fixture(scope="module")
def panel_interim():
    """E：年报每股 2.0 连续三年；2024 年首次中报 1.0（已除息）。预测 2024 年报应 ≈ 全年 − 1.0"""
    conn = db.connect(":memory:")
    days = pd.bdate_range("2021-01-01", "2025-12-31").strftime("%Y-%m-%d")
    db.upsert(conn, "index_daily", pd.DataFrame({"product": P, "date": days, "close": S}))
    db.upsert(conn, "constituent_daily", pd.DataFrame(
        [{"product": P, "date": d, "stock_code": "E", "close": PX} for d in days]))
    db.upsert(conn, "index_weight", pd.DataFrame([{"product": P, "date": "2024-11-29", "stock_code": "E", "weight": 100.0}]))
    db.upsert(conn, "dividend_announcement", pd.DataFrame([
        _ev("E", "2022-06-15", 2.0, "2022-04-15", "2021-12-31"),
        _ev("E", "2023-06-15", 2.0, "2023-04-15", "2022-12-31"),
        _ev("E", "2024-06-14", 2.0, "2024-04-15", "2023-12-31"),
        _ev("E", "2024-12-13", 1.0, "2024-10-30", "2024-06-30"),
    ]))
    return D.Panel(conn, P)


def test_annual_net_of_interim(panel_interim):
    rows = [r for r in D.predict_stocks(panel_interim, "2024-12-20", detail=True) if r["报告期"] == "12-31"]
    assert len(rows) == 1
    assert rows[0]["dps"] == pytest.approx(1.0)                  # 全年 2.0 − 已分中报 1.0
    assert rows[0]["金额依据"].endswith("年内已分")
    old = [r for r in D.predict_stocks(panel_interim, "2024-12-20", D.Params(annual_by_fy=False), detail=True)
           if r["报告期"] == "12-31"]
    assert old[0]["dps"] == pytest.approx(2.0)                   # 旧逻辑：年报按自身历史 → 重复计入
