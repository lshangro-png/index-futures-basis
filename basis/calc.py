"""
calc.py —— 基差计算：期权链配对 → PCP 反推远期 → 期货/期权基差、年化、分红修正

    res = analyze_product(conn, "300", start="2022-07-01")

输出一行一天的宽表，列名 {slot}_{指标}，slot 取 00/01/02/03。主要指标：

    basis / basis_exdiv / basis_exdiv_pred    期货基差：含分红 / 除已实现分红 / 除预测分红
    ann / ann_exdiv / ann_exdiv_pred          对应年化(%)
    div / div_pred / div_err                  区间分红：已实现 / 预测 / 预测−已实现
    basis_opt / basis_opt_pred / basis_opt_exdiv, ann_opt…
                                              ETF期权隐含：减已实现分红 / 减预测分红 / 不含分红
    basis_iopt / basis_iopt_exdiv / basis_iopt_exdiv_pred, ann_iopt…
                                              股指期权隐含：含分红 / 除已实现分红 / 除预测分红
    opt_minus_fut / opt_minus_fut_pred / iopt_minus_fut
                                              期权隐含 − 期货（同口径：都含分红）

凡是带 _pred 的列只用当日可知信息，可以回测；用了已实现分红(div)的列含前视偏差。
ETF 的 basis_opt_exdiv 和股指期权的 basis_iopt 不依赖分红，本身就没有前视偏差。
"""

import re

import numpy as np
import pandas as pd

from . import config as C
from . import db
from .config import PRODUCTS, SLOTS


# ================================================================== 期权链

def parse_etf_contract(name):
    """ETF期权中文合约名 → (购沽, 年, 月, 行权价, 是否调整合约)"""
    s = str(name).strip()
    if not s or s == "nan":
        return None
    typ = "C" if "购" in s else ("P" if "沽" in s else None)
    if typ is None:
        return None
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", s)
    if m:
        yy, mm, tail = int(m.group(1)), int(m.group(2)), s[m.end():]
    else:
        m = re.search(r"(\d{1,2})\s*月", s)
        if not m:
            return None
        yy, mm, tail = None, int(m.group(1)), s[m.end():]
    t = re.search(r"(\d+)\s*([A-Z]?)\s*$", tail)
    if not t:
        return None
    raw = t.group(1)
    strike = float(raw) / 1000 if len(raw) >= 4 else float(raw)
    return typ, yy, mm, strike, bool(t.group(2))


def nth_weekday(y, m, weekday, n):
    """某月第 n 个星期几（weekday: 周一=0）。ETF期权=第4个周三，股指期权=第3个周五"""
    days = [d for d in pd.date_range(pd.Timestamp(y, m, 1), periods=31, freq="D")
            if d.weekday() == weekday and d.month == m]
    return days[n - 1] if len(days) >= n else None


def etf_option_expiry(y, m, cal=None):
    """ETF期权到期日：到期月第4个周三，遇非交易日顺延。cal 为交易日 DatetimeIndex，超出其范围则不顺延"""
    d = nth_weekday(y, m, 2, 4)
    if d is not None and cal is not None and len(cal) and d <= cal[-1]:
        d = cal[cal.searchsorted(d)]
    return d


def dividend_cum(dpi):
    """股息点指数 → 累计已实现分红点数（跨年重设处接上），区间分红 = 两端差值"""
    if dpi is None or dpi.empty:
        return None
    d = dpi.sort_values("date").set_index("date")["value"].astype(float)
    inc = d.diff()
    reset = inc < -1                        # 每年12月重设归零：当天值就是归零后当天的分红
    inc[reset] = d[reset]
    return inc.fillna(0.0).cumsum()


def _window_div(cum, a, b):
    """(a, b] 区间已实现分红；b 超出股息点数据范围（尚未发生）时为 NaN"""
    if cum is None or b > cum.index[-1] or a < cum.index[0]:
        return np.nan
    return float(cum.asof(b) - cum.asof(a))


def _pair_calls_puts(d, keys, vol_cols):
    ren = lambda t: {"close": f"{t}_close", vol_cols[0]: f"{t}_volume", vol_cols[1]: f"{t}_notional"}
    cols = keys + ["close", *vol_cols]
    call = d[d["typ"] == "C"][cols].rename(columns=ren("call")).drop_duplicates(keys)
    put = d[d["typ"] == "P"][cols].rename(columns=ren("put")).drop_duplicates(keys)
    return call.merge(put, on=keys, how="inner")


def build_etf_chain(q, verbose=True):
    """ETF期权逐合约行情 → 按 (日期, 到期日, 行权价, 是否调整) 配对的购沽链"""
    if q is None or q.empty:
        return pd.DataFrame()
    d = q.copy()
    d["date"] = pd.to_datetime(d["date"])
    for c in ("close", "volume", "notional"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    d = d.dropna(subset=["close"])
    d = d[d["close"] > 0]
    if d.empty:
        return pd.DataFrame()

    # 成交额单位识别：成交额/成交量/价格 应≈合约乘数，远小于它说明是万元
    m = d[(d["volume"] > 0) & (d["notional"] > 0)]
    if len(m) >= 100:
        imp = (m["notional"] / m["volume"] / m["close"]).median()
        if np.isfinite(imp) and imp < 100:
            d["notional"] *= C.CONTRACT_MULTIPLIER
            if verbose:
                print(f"    ℹ 成交额判定为万元(比值={imp:.2f})，已×{C.CONTRACT_MULTIPLIER}")

    info = d["contract_name"].apply(parse_etf_contract)
    d = d[info.notna()].copy()
    info = info[info.notna()]
    if d.empty:
        return pd.DataFrame()
    d["typ"], d["yy"], d["mm"], d["strike"], d["adj"] = zip(*info)

    # 到期日 = 该合约最后一个有行情的日子；仍在交易的合约按第4个周三推算
    d["expiry"] = d["option_code"].map(d.groupby("option_code")["date"].max())
    need = d["expiry"] >= d["date"].max() - pd.Timedelta(days=3)
    if need.any():
        fb = []
        for _, r in d[need].iterrows():
            if pd.isna(r["mm"]):
                fb.append(r["expiry"]); continue
            mm = int(r["mm"])
            yy = int(r["yy"]) if pd.notna(r["yy"]) else r["date"].year + (1 if mm < r["date"].month else 0)
            w = nth_weekday(yy, mm, 2, 4)
            fb.append(w if w is not None else r["expiry"])
        d.loc[need, "expiry"] = fb

    ch = _pair_calls_puts(d, ["date", "expiry", "strike", "adj"], ("volume", "notional"))
    ch["contract_month"] = pd.to_datetime(ch["expiry"]).dt.strftime("%y%m")
    ch = ch.sort_values(["date", "expiry", "strike"]).reset_index(drop=True)
    if verbose and len(ch):
        print(f"    ✓ ETF期权链 {len(ch)} 组配对，{ch['date'].nunique()} 个交易日")
    return ch


def build_index_chain(q, verbose=True):
    """股指期权逐合约行情 → 按 (日期, 到期月, 行权价) 配对。到期日=每月第3个周五"""
    if q is None or q.empty:
        return pd.DataFrame()
    d = q.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d["strike"] = pd.to_numeric(d["strike"], errors="coerce")
    d = d.dropna(subset=["close", "strike", "opt_type", "contract_month"])
    d = d[d["close"] > 0].rename(columns={"opt_type": "typ"})
    if d.empty:
        return pd.DataFrame()
    ch = _pair_calls_puts(d, ["date", "contract_month", "strike"], ("volume", "turnover"))
    if ch.empty:
        return ch
    exp = {cm: nth_weekday(2000 + int(cm[:2]), int(cm[2:]), 4, 3) for cm in ch["contract_month"].unique()}
    ch["expiry"] = ch["contract_month"].map(exp)
    ch = ch.dropna(subset=["expiry"]).sort_values(["date", "expiry", "strike"]).reset_index(drop=True)
    if verbose and len(ch):
        print(f"    ✓ 股指期权链 {len(ch)} 组配对，{ch['date'].nunique()} 个交易日")
    return ch


# ================================================================== PCP / ETF除息

def implied_forward(sl, r, T, spot, call_col="call_close", put_col="put_close"):
    """PCP 反推远期：F = K + (C − P)·e^{rT}，取平值上下各 N 档的中位数"""
    if sl is None or len(sl) == 0 or not np.isfinite(spot) or spot <= 0:
        return np.nan, 0
    df = sl.dropna(subset=[call_col, put_col, "strike"])
    df = df[(df[call_col] > 0) & (df[put_col] > 0)]
    if df.empty:
        return np.nan, 0
    near = pd.concat([df[df["strike"] <= spot].nlargest(C.N_STRIKES_SIDE, "strike"),
                      df[df["strike"] > spot].nsmallest(C.N_STRIKES_SIDE, "strike")])
    if near.empty:
        return np.nan, 0
    fw = near["strike"] + (near[call_col] - near[put_col]) * np.exp(r * max(T, 1e-6))
    fw = fw[np.isfinite(fw)]
    return (float(fw.median()), int(len(fw))) if len(fw) else (np.nan, 0)


def detect_etf_dist(idx, etf, verbose=True):
    """用 指数/ETF 比值的阶跃识别 ETF 自身除息日与金额"""
    if idx is None or etf is None or idx.empty or etf.empty:
        return pd.DataFrame(columns=["ex_date", "amount"])
    m = idx.merge(etf.rename(columns={"close": "etf_close"}), on="date").sort_values("date").reset_index(drop=True)
    m["ratio"] = m["index_close"] / m["etf_close"]
    m["jump"] = m["ratio"].pct_change() * 100
    hits = []
    for k in m.index[(m["jump"] > C.ETF_JUMP_MIN) & (m["jump"] <= C.ETF_JUMP_MAX)]:
        j = m["jump"].iloc[k]
        b, a = m["ratio"].iloc[max(0, k - 6):k].median(), m["ratio"].iloc[k:k + 6].median()
        exp = b * j / 100
        if not np.isfinite(b) or not np.isfinite(a) or exp <= 0 or (a - b) / exp < C.ETF_KEEP:
            continue
        noise = m["jump"].iloc[max(0, k - 20):k].std()
        if np.isfinite(noise) and noise > 0 and j / noise < C.ETF_SNR:
            continue
        hits.append((k, m["date"].iloc[k], j, m["etf_close"].iloc[k]))
    hits.sort(key=lambda t: -t[2])
    kept = []
    for h in hits:
        if all(abs(h[0] - x[0]) >= C.ETF_GAP for x in kept):
            kept.append(h)
    kept.sort(key=lambda t: t[0])
    r = pd.DataFrame([{"ex_date": dt, "amount": px * (j / 100) / (1 + j / 100)} for _, dt, j, px in kept],
                     columns=["ex_date", "amount"])
    if verbose and len(r):
        print("    ℹ ETF除息 %d 次: %s" % (len(r), ", ".join(
            f"{pd.Timestamp(d).date()}({a:.3f})" for d, a in zip(r["ex_date"], r["amount"]))))
    return r


# ================================================================== 基差

def _ann(x, S, dte):
    return x / S * C.DAYS_PER_YEAR / dte * 100


def _fut_month(code):
    """期货合约代码 → 到期年月 'YYMM'（IF2503 → '2503'），用来找同月到期的期权"""
    m = re.search(r"(\d{4})$", str(code))
    return m.group(1) if m else None


def _cont_rate(shibor, T):
    """SHIBOR(单利, 小数) → 连续复利"""
    return np.log(1 + shibor * T) / T if (np.isfinite(shibor) and T > 0 and 1 + shibor * T > 0) else 0.0


def compute(idx, fut, etf, sh, etf_chain, index_chain, dist, verbose=True, div_cum=None):
    """div_cum：累计已实现分红（dividend_cum），用于按 ETF 期权自己的到期日算区间分红"""
    out = idx.merge(fut, on="date").merge(sh, on="date", how="left")
    out = (out.merge(etf.rename(columns={"close": "etf_close"}), on="date", how="left")
           if not etf.empty else out.assign(etf_close=np.nan))
    out["shibor"] = out["shibor"].ffill()
    out["etf_to_index"] = out["index_close"] / out["etf_close"]

    ecbd = {d: g for d, g in etf_chain.groupby("date")} if len(etf_chain) else {}
    icbd = {d: g for d, g in index_chain.groupby("date")} if len(index_chain) else {}
    dist_dates = pd.to_datetime(dist["ex_date"]).reset_index(drop=True) if len(dist) else None
    # ETF 自身分红在 T 日是否已知：除息日前 ETF_DIST_NOTICE_DAYS 个交易日起才算公告了（point-in-time）
    if dist_dates is not None:
        tdays = pd.DatetimeIndex(sorted(out["date"].unique()))
        pos = tdays.searchsorted(dist_dates)
        dist_known = pd.Series([tdays[max(i - C.ETF_DIST_NOTICE_DAYS, 0)] for i in pos])
        dist_amt = dist["amount"].reset_index(drop=True)

    recs = []
    for _, row in out.iterrows():
        S, dt = row["index_close"], row["date"]
        if not np.isfinite(S) or S <= 0:
            continue
        rec = {"date": dt, "spot": S, "etf_close": row.get("etf_close"), "shibor": row.get("shibor")}
        ec, ic = ecbd.get(dt), icbd.get(dt)
        sb = row.get("shibor")

        # 期权按【到期月】对齐期货，不能按"第几个到期"：
        #   股指期权挂 当月+下2月+3个季月，期货挂 当月+次月+2个季月，序号在季月位次上错开；
        #   ETF期权第4个周三到期，期货第3个周五，两者之间那几天 ETF 当月还没到期、期货已换月。
        # 找不到同月期权的位次（如上述那几天的期货次月）留空，不拿别的月份凑。
        for s in SLOTS:
            F, ltd = row.get(f"close_{s}"), row.get(f"last_trading_date_{s}")
            ym = _fut_month(row.get(f"contract_code_{s}"))
            if not np.isfinite(F) or F <= 0:
                continue
            # 到期日当天用结算价（交割价口径）
            if pd.notna(ltd) and pd.Timestamp(ltd).normalize() == dt.normalize():
                st = row.get(f"settle_{s}")
                if np.isfinite(st) and st > 0:
                    F = st
            dte = row.get(f"days_{s}")
            if not np.isfinite(dte) or dte <= 0:
                dte = ((pd.Timestamp(ltd) - dt).days + 1) if pd.notna(ltd) else np.nan
            # div      = 事后已实现分红（股息点指数区间差值），含前视偏差，只能描述历史
            # div_pred = 当日可知信息下的预测分红（dividend_forecast_daily），可用于回测
            div = row.get(f"div_points_{s}")
            dpred = row.get(f"{s}_div_pred", np.nan)
            has_div, has_pred = np.isfinite(div), np.isfinite(dpred)
            basis = F - S
            rec.update({f"{s}_F": F, f"{s}_DTE": dte, f"{s}_div": div, f"{s}_basis": basis,
                        f"{s}_div_pred": dpred,
                        f"{s}_basis_exdiv": basis + div if has_div else np.nan,
                        f"{s}_basis_exdiv_pred": basis + dpred if has_pred else np.nan,
                        f"{s}_div_err": dpred - div if (has_pred and has_div) else np.nan})
            if np.isfinite(dte) and dte > 0:
                rec[f"{s}_ann"] = _ann(basis, S, dte)
                if has_div:
                    rec[f"{s}_ann_exdiv"] = _ann(basis + div, S, dte)
                if has_pred:
                    rec[f"{s}_ann_exdiv_pred"] = _ann(basis + dpred, S, dte)

            # ---- ETF期权：ETF 在成分股除息时不跌（现金留在基金里，到ETF自身除息才分），
            # 所以 ETF 远期换算成指数点位后是"不含分红"的，要减去区间分红才和期货同口径
            sl = ec[ec["contract_month"] == ym] if (ec is not None and ym) else None
            if sl is not None and len(sl) and np.isfinite(row.get("etf_close", np.nan)):
                oe = pd.Timestamp(sl["expiry"].max())
                sl = sl[sl["expiry"] == oe]
                odte = (oe - dt).days + 1
                T = odte / C.DAYS_PER_YEAR
                f_etf, _ = implied_forward(sl, _cont_rate(sb, T), T, row["etf_close"])
                if np.isfinite(f_etf):
                    # 期权窗口内的 ETF 自身分红要加回。只加 T 日已公告的：
                    # 还没公告的当日不可知，按 0 处理 —— 窗口跨 ETF 除息日时 ETF 版会偏低几十点，
                    # 直到公告日跳回，这是不用未来信息的代价
                    add = float(dist_amt[(dist_dates > dt) & (dist_dates <= oe) & (dist_known <= dt)].sum()) \
                        if dist_dates is not None else 0.0
                    # raw = 不含分红的指数远期基差，不依赖成分股分红 → 就是 exdiv 版
                    raw = (f_etf + add) * row["etf_to_index"] - S
                    # 区间分红按【期权自己的到期日】算：ETF期权比同月期货晚几天到期
                    div_o = _window_div(div_cum, dt, oe) if div_cum is not None else div
                    dpred_o = row.get(f"{s}_div_pred_etf", np.nan) if f"{s}_div_pred_etf" in row.index else dpred
                    has_div_o, has_pred_o = np.isfinite(div_o), np.isfinite(dpred_o)
                    rec.update({f"{s}_opt_DTE": odte, f"{s}_basis_opt_exdiv": raw,
                                f"{s}_opt_div": div_o, f"{s}_opt_div_pred": dpred_o})
                    # 含分红口径要减分红：已实现版含前视偏差，另出一份减【预测】分红的，可回测。
                    # 没有已实现分红（样本末端）就留空，不能悄悄当 0 减，否则同一列混两种口径
                    if has_div_o:
                        bo = raw - div_o
                        rec.update({f"{s}_basis_opt": bo, f"{s}_opt_minus_fut": bo - basis})
                    if has_pred_o:
                        rec.update({f"{s}_basis_opt_pred": raw - dpred_o,
                                    f"{s}_opt_minus_fut_pred": raw - dpred_o - basis})
                    # 年化统一口径：期权用期权自己的DTE，不是期货的
                    if odte > 0:
                        rec[f"{s}_ann_opt_exdiv"] = _ann(raw, S, odte)
                        if has_div_o:
                            rec[f"{s}_ann_opt"] = _ann(raw - div_o, S, odte)
                        if has_pred_o:
                            rec[f"{s}_ann_opt_pred"] = _ann(raw - dpred_o, S, odte)
                    for src, dst in [("volume", "tradeable_vol"), ("notional", "tradeable_notional")]:
                        t = sl.dropna(subset=[f"call_{src}", f"put_{src}"])
                        if len(t):
                            rec[f"{s}_{dst}"] = float(np.minimum(t[f"call_{src}"].values,
                                                                 t[f"put_{src}"].values).sum() * C.TRADEABLE_RATIO)

            # ---- 股指期权：标的就是价格指数，PCP 反推的远期【已经】反映了区间分红，
            # 与期货同口径，不能再减分红（旧版多减了一次，导致分红季 iopt−期货 ≈ −分红）
            sl = ic[ic["contract_month"] == ym] if (ic is not None and ym) else None
            if sl is not None and len(sl):
                # 股指期权与股指期货同为第3个周五到期、遇假顺延，直接用期货最后交易日，
                # 比自己推算的第3个周五更准（推算没处理节假日）
                oe = pd.Timestamp(ltd) if pd.notna(ltd) else pd.Timestamp(sl["expiry"].iloc[0])
                odte = (oe - dt).days + 1
                T = odte / C.DAYS_PER_YEAR
                f_idx, _ = implied_forward(sl, _cont_rate(sb, T), T, S)
                if np.isfinite(f_idx):
                    bi = f_idx - S
                    rec.update({f"{s}_iopt_DTE": odte, f"{s}_basis_iopt": bi,
                                f"{s}_iopt_minus_fut": bi - basis})
                    if odte > 0:
                        rec[f"{s}_ann_iopt"] = _ann(bi, S, odte)
                        if has_div:
                            rec[f"{s}_basis_iopt_exdiv"] = bi + div
                            rec[f"{s}_ann_iopt_exdiv"] = _ann(bi + div, S, odte)
                        if has_pred:
                            rec[f"{s}_basis_iopt_exdiv_pred"] = bi + dpred
                            rec[f"{s}_ann_iopt_exdiv_pred"] = _ann(bi + dpred, S, odte)
        recs.append(rec)

    res = pd.DataFrame(recs)
    if verbose and len(res):
        print(f"  ✓ {len(res)} 行 ({res['date'].min().date()} ~ {res['date'].max().date()})")
    return res


def attach_forecast(conn, product, fut, verbose=True):
    """把 dividend_forecast_daily 拼成 {slot}_div_pred 列并入期货宽表。表为空时跳过"""
    wide = db.load_forecast_wide(conn, product)
    if wide.empty:
        if verbose:
            print("  · 无预测分红表，跳过（先跑 python -m basis precompute）")
        return fut
    out = fut.merge(wide, on="date", how="left")
    if verbose:
        got = [c for c in out.columns if c.endswith("_div_pred")]
        n = int(out[got].notna().any(axis=1).sum()) if got else 0
        print(f"  · 预测分红：{len(got)}个位次，{n}/{len(out)} 天有值")
    return out


def analyze_product(conn, product, start=None, end=None, verbose=True):
    """一个品种的完整流程：取数 → 建期权链 → 识别ETF除息 → 并入预测分红 → 算基差"""
    cfg = PRODUCTS[product]
    if verbose:
        print(f"[{cfg['label']}（{product}）]")
    idx = db.load_index(conn, product, start, end)
    fut = db.load_futures_wide(conn, product, start, end)
    if idx.empty or fut.empty:
        if verbose:
            print("  ✗ 无指数或期货数据，跳过")
        return pd.DataFrame()
    etf = db.load_series(conn, "etf_daily", product, "close", start, end)
    sh = db.load_shibor(conn, "shibor_3m", start, end)
    oq = db.load_options(conn, product, start, end)
    ioq = db.load_index_options(conn, product, start, end) if cfg.get("idx_opt") else pd.DataFrame()

    etf_chain = build_etf_chain(oq, verbose) if len(oq) else pd.DataFrame()
    index_chain = build_index_chain(ioq, verbose) if len(ioq) else pd.DataFrame()
    dist = detect_etf_dist(idx, etf, verbose) if len(etf) else pd.DataFrame(columns=["ex_date", "amount"])
    div_cum = dividend_cum(db.load_series(conn, "dividend_point_index", product, "value")) if len(oq) else None
    fut = attach_forecast(conn, product, fut, verbose)
    return compute(idx, fut, etf, sh, etf_chain, index_chain, dist, verbose, div_cum=div_cum)


def analyze_all(conn, start=None, end=None, products=None, verbose=True):
    results = {}
    for p in products or PRODUCTS:
        r = analyze_product(conn, p, start, end, verbose)
        if len(r):
            results[p] = r
        if verbose:
            print()
    return results
