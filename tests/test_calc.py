"""calc.py 口径测试：期权按到期月对齐期货"""

import numpy as np
import pandas as pd
import pytest

from basis import calc
from basis.calc import nth_weekday

DT = pd.Timestamp("2024-11-11")
SPOT = 4000.0
# 期货：当月/次月/两个季月；股指期权：当月 + 下2月 + 季月 —— 第3个起序号错开
FUT = {"00": "2411", "01": "2412", "02": "2503", "03": "2506"}
OPT_MONTHS = ["2411", "2412", "2501", "2503", "2506", "2509"]
# 每个月份给一个可辨认的远期，测试能看出取的是哪个月
FWD = {"2411": 3990.0, "2412": 3970.0, "2501": 3950.0, "2503": 3920.0, "2506": 3880.0, "2509": 3850.0}


def _expiry(ym):
    return nth_weekday(2000 + int(ym[:2]), int(ym[2:]), 4, 3)


def _inputs():
    idx = pd.DataFrame({"date": [DT], "index_close": [SPOT]})
    fut = {"date": [DT]}
    for s, ym in FUT.items():
        ltd = _expiry(ym)
        fut.update({f"close_{s}": [FWD[ym]], f"settle_{s}": [np.nan], f"last_trading_date_{s}": [ltd],
                    f"days_{s}": [(ltd - DT).days + 1], f"div_points_{s}": [np.nan],
                    f"contract_code_{s}": [f"IF{ym}"]})
    fut = pd.DataFrame(fut)
    sh = pd.DataFrame({"date": [DT], "shibor": [0.0]})          # r=0：PCP 远期 = K + C − P
    rows = []
    for ym in OPT_MONTHS:
        for k in np.arange(3800, 4201, 50):
            c = max(FWD[ym] - k, 0) + 20
            rows.append({"date": DT, "contract_month": ym, "strike": float(k), "expiry": _expiry(ym),
                         "call_close": c, "put_close": c - (FWD[ym] - k),
                         "call_volume": 1, "put_volume": 1, "call_turnover": 1, "put_turnover": 1})
    ichain = pd.DataFrame(rows)
    dist = pd.DataFrame(columns=["ex_date", "amount"])
    return idx, fut, pd.DataFrame(), sh, pd.DataFrame(), ichain, dist


def test_fut_month():
    assert calc._fut_month("IF2503") == "2503"
    assert calc._fut_month("MO2412") == "2412"
    assert calc._fut_month(None) is None


def test_index_option_aligned_by_contract_month():
    res = calc.compute(*_inputs(), verbose=False).iloc[0]
    for s, ym in FUT.items():
        # 同月 → 期权隐含基差 = 期货基差，到期日相同
        assert res[f"{s}_iopt_DTE"] == res[f"{s}_DTE"], s
        assert abs(res[f"{s}_iopt_minus_fut"]) < 1e-6, s
        assert abs(res[f"{s}_basis_iopt"] - (FWD[ym] - SPOT)) < 1e-6, s


def test_missing_month_left_empty():
    idx, fut, etf, sh, ec, ichain, dist = _inputs()
    ichain = ichain[ichain["contract_month"] != "2503"]          # 季月1 没有同月期权
    res = calc.compute(idx, fut, etf, sh, ec, ichain, dist, verbose=False).iloc[0]
    assert pd.isna(res.get("02_basis_iopt"))                     # 留空，不拿别的月份凑
    assert abs(res["03_iopt_minus_fut"]) < 1e-6


def test_etf_option_expiry_rolls_over_holiday():
    # 2024-10 第4个周三是 10/23；构造一个 10/23 休市的日历，应顺延到 10/24
    cal = pd.DatetimeIndex(pd.bdate_range("2024-10-01", "2024-10-31").drop(pd.Timestamp("2024-10-23")))
    assert calc.etf_option_expiry(2024, 10) == pd.Timestamp("2024-10-23")
    assert calc.etf_option_expiry(2024, 10, cal) == pd.Timestamp("2024-10-24")


def test_dividend_cum_keeps_reset_day():
    # 股息点指数 12/16 归零，当天又有 1.5 点分红 → 区间分红不能丢这 1.5
    dpi = pd.DataFrame({"date": pd.to_datetime(["2025-12-12", "2025-12-15", "2025-12-16", "2025-12-17"]),
                        "value": [80.0, 81.0, 1.5, 2.0]})
    cum = calc.dividend_cum(dpi)
    assert calc._window_div(cum, pd.Timestamp("2025-12-12"), pd.Timestamp("2025-12-17")) == pytest.approx(3.0)
    assert np.isnan(calc._window_div(cum, pd.Timestamp("2025-12-12"), pd.Timestamp("2025-12-31")))  # 尚未发生
