"""
dividend.py —— 分红预测层（德邦金工报告 2024-07-12 方法复现）

在历史某个时点 T，只用当时已公开的信息，预测 T 到期货到期日之间会发生多少分红点数：

    分红点数 = Σ (每股分红 / 股价) × 成分股权重 × 指数点位

point-in-time 纪律（这是全部意义所在）：
  - 分红事件在 T 日是否"已知"，看 plan_date <= T，不是看 ex_date
  - 除息日在 T 日是否"确定"，看 impl_date <= T（实施公告才公布除息日）
  - 财报在 T 日是否"可见"，看 stock_financials.pub_date <= T，不是 report_date
违反任何一条都会引入前视偏差，回测结果会虚高。

本模块是预测逻辑的【唯一实现】：单点预测、历史回测、逐日预计算落库、
对账(verify reconcile) 都走这里的 Panel + predict_stocks，不再各写一份。
"""

import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from . import config as C
from .calc import etf_option_expiry


@dataclass(frozen=True)
class Params:
    lookback_years: int = C.LOOKBACK_YEARS
    min_occurrence: int = C.MIN_OCCURRENCE
    # 以下开关只为做对照实验，正常都开着
    annual_by_fy: bool = True       # 年报那笔 = 全年合计预测 − 同年度中报/季报
    # 报告 3.3：不满足线性时用 净利润 × 股利支付率。回测（50/300/500，2016–2026）反而略差
    # （300 的 90 天误差均值 0.25 → 0.59，500 的 90 天 ≤3点占比 81% → 77%），默认关闭
    profit_path: bool = False
    consecutive: bool = True        # 报告 3.2：线性判断要求连续三年都分红


# ================================================================== 规则

def is_stable(gaps, threshold):
    """报告图16：最大偏离度 MAD = max(|gap_i - mean|)，小于阈值即稳定"""
    g = pd.Series(gaps).dropna()
    if len(g) < 2:
        return False, np.nan
    mad = float((g - g.mean()).abs().max())
    return mad < threshold, g.mean()


def default_exdate(asof):
    """报告 2.3 步骤(6)：无法预测时的默认除息日

    依据是"91%的分红在7月底前完成"+"97%的公司在除息前10天发实施公告"。
    超过10月21日即将披露三季报，进入下一轮周期，视为今年不分红。
    """
    t = pd.Timestamp(asof)
    y = t.year
    for cut, tgt in [((y, 7, 21), (y, 7, 31)), ((y, 8, 21), (y, 8, 31)),
                     ((y, 9, 20), (y, 9, 30)), ((y, 10, 21), (y, 10, 31))]:
        if t < pd.Timestamp(*cut):
            return pd.Timestamp(*tgt)
    return None          # 今年不分红


def to_friday(d):
    """报告 2.3 步骤(7)：A股年报除息偏好周五，预测日落在周末则修正到前一个周五"""
    if d is None or pd.isna(d):
        return d
    wd = d.weekday()
    if wd >= 5:
        return d - timedelta(days=wd - 4)
    return d


def _roll_forward(last, a):
    """历史除息日按年滚动到晚于 a（2月29日滚到非闰年取28日）。当日除息的已发生，滚到明年"""
    d = last
    while d <= a:
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            d = d.replace(year=d.year + 1, day=28)
    return d


def predict_exdate(hist, cur, asof, market_gaps=None):
    """预测单笔分红的除息日。hist=该股同报告期历史事件(可能为空), cur=当前周期事件(可能为空)

    market_gaps = {"gap_plan": 天数, "gap_sgm": 天数}：全市场历史间隔中位数，
    该股在这个报告期从没分过红（如首次中期分红）时，用它代替个股间隔外推。
    返回 (预测日, 依据) —— 依据用于诊断，能看出各档命中比例
    """
    a = pd.Timestamp(asof)

    # (1) 实施公告已发 → 除息日已公布，直接用
    if (cur is not None and pd.notna(cur.get("impl_date")) and pd.Timestamp(cur["impl_date"]) <= a
            and pd.notna(cur.get("ex_date"))):
        return pd.Timestamp(cur["ex_date"]), "已公布"

    tag = cur.get("period_tag") if cur is not None else None
    same_period = hist[hist["period_tag"] == tag] if tag else hist
    if same_period.empty:
        same_period = hist
    mg = market_gaps if same_period.empty else None
    # 历史除息日：同报告期最近一次，按年滚动到晚于 asof。
    # 不能简单 replace(year=asof.year) —— 若今年这轮已经除过息，会漏掉下一轮。
    hist_ex = _roll_forward(pd.Timestamp(same_period.iloc[-1]["ex_date"]), a) if len(same_period) else None

    # (4) 已出决案未出实施公告 → 决案+间隔外推
    if cur is not None and pd.notna(cur.get("sgm_date")) and pd.Timestamp(cur["sgm_date"]) <= a:
        ok, mean_gap = is_stable(same_period["gap_sgm"].tail(3), C.MAD_SGM)
        if ok:
            return pd.Timestamp(cur["sgm_date"]) + timedelta(days=float(mean_gap)), "决案外推"
        if mg and np.isfinite(mg.get("gap_sgm", np.nan)):
            return pd.Timestamp(cur["sgm_date"]) + timedelta(days=float(mg["gap_sgm"])), "决案+市场间隔"

    # (3) 已出预案未出决案 → 预案+间隔外推
    if cur is not None and pd.notna(cur.get("plan_date")) and pd.Timestamp(cur["plan_date"]) <= a:
        ok, mean_gap = is_stable(same_period["gap_plan"].tail(3), C.MAD_PLAN)
        if ok:
            return pd.Timestamp(cur["plan_date"]) + timedelta(days=float(mean_gap)), "预案外推"
        if mg and np.isfinite(mg.get("gap_plan", np.nan)):
            return pd.Timestamp(cur["plan_date"]) + timedelta(days=float(mg["gap_plan"])), "预案+市场间隔"

    # (2) 连预案都没出 / 间隔不稳定 → 历史除息日
    if hist_ex is not None:
        return hist_ex, "历史除息日"
    return None, "无依据"


def yearly_dps(hist):
    """按报告期年份合计每股分红（同一报告期分几次派的合并）→ Series[年份]"""
    h = hist[hist["div_per_share"] > 0]
    return h.groupby(h["report_date"].dt.year)["div_per_share"].sum().sort_index()


def extrapolate(d, consecutive=True):
    """报告 3.2：每股股利线性外推。d = 按年份的每股分红 Series

    D_error = ((D_{t-1} + D_{t-3})/2 - D_{t-2}) / D_{t-2}
    最近三年连续分红、且误差小于阈值，即认为具有线性关系，线性外推；否则退回上一年值。
    """
    if len(d) == 0:
        return np.nan, "从未分红"
    d1 = float(d.iloc[-1])
    t = d.tail(3)
    if len(t) < 3 or (consecutive and t.index[-1] - t.index[0] != 2):
        return d1, "沿用上年"
    d3, d2 = float(t.iloc[0]), float(t.iloc[1])
    if d2 == 0:
        return d1, "沿用上年"
    err = abs(((d1 + d3) / 2 - d2) / d2)
    if err < C.DPS_LINEAR_TOL:
        return max(d1 + (d1 - d3) / 2, 0.0), "线性外推"
    return d1, "沿用上年"


def predict_dps(hist, consecutive=True):
    """某报告期的下一笔每股分红（按该报告期历年金额外推）"""
    return extrapolate(yearly_dps(hist), consecutive)


def np_forecast(fin, fy):
    """报告 3.3.1：预测 fy 年度归母净利润。fin = 该股 asof 当时已披露的财务 {(年, 'MM-DD'): 净利润}

    年报已出 → 实际值；否则要有 fy 三季报：
      过去三年「前三季度/全年」比例稳定（最大偏离 < NP_RATIO_TOL）→ 三季报 / 平均比例
      不稳定 → 三季报 + 上年第四季度
    没有三季报（报告也是三季报后才开始测算）→ None
    """
    if (fy, "12-31") in fin:
        return fin[(fy, "12-31")]
    q3 = fin.get((fy, "09-30"))
    if q3 is None:
        return None
    props = [fin[(y, "09-30")] / fin[(y, "12-31")] for y in (fy - 1, fy - 2, fy - 3)
             if (y, "09-30") in fin and fin.get((y, "12-31"), 0) > 0]
    if len(props) == 3:
        m = float(np.mean(props))
        if m > 0 and max(abs(x - m) for x in props) < C.NP_RATIO_TOL:
            return q3 / m
    if (fy - 1, "09-30") in fin and (fy - 1, "12-31") in fin:
        return q3 + fin[(fy - 1, "12-31")] - fin[(fy - 1, "09-30")]
    return None


def profit_scaled_total(totals, fin, fy, div_total_by_year):
    """报告 3.3：全年分红 = 净利润预测 × 股利支付率，支付率取最近一个分红年度的（>100% 截断）。

    股本不变时等价于 最近一年每股分红合计 × 净利润增速，不需要股本数据。返回 None 表示数据不够
    """
    if len(totals) == 0:
        return None
    y = int(totals.index[-1])
    np_y, np_f = fin.get((y, "12-31")), np_forecast(fin, fy)
    if np_f is None or np_y is None or np_y <= 0:
        return None
    if np_f <= 0:
        return 0.0
    tot = float(totals.iloc[-1]) * np_f / np_y
    payout = div_total_by_year.get(y, np.nan) / np_y
    if np.isfinite(payout) and payout > 1:
        tot /= payout
    return tot


# ================================================================== 数据

def load_events(conn, product, positive_only=False):
    sql = ("SELECT stock_code, ex_date, div_per_share, div_total, plan_date, sgm_date, impl_date, "
           "report_date FROM dividend_announcement WHERE product=?")
    if positive_only:
        sql += " AND div_per_share > 0"
    ev = pd.read_sql(sql + " ORDER BY stock_code, ex_date", conn, params=[product])
    for c in ["ex_date", "plan_date", "sgm_date", "impl_date", "report_date"]:
        ev[c] = pd.to_datetime(ev[c], errors="coerce")
    ev = ev.dropna(subset=["ex_date"])
    ev["period_tag"] = ev["report_date"].dt.strftime("%m-%d")     # 年报/中报，同类才可比
    ev["gap_plan"] = (ev["ex_date"] - ev["plan_date"]).dt.days
    ev["gap_sgm"] = (ev["ex_date"] - ev["sgm_date"]).dt.days
    return ev


def load_pending(conn, product):
    """在途预案：已出预案、还没定除息日。结构与 load_events 对齐（ex_date 为空）"""
    pe = pd.read_sql("SELECT stock_code, report_date, plan_date, sgm_date, impl_date, "
                     "div_per_share, stage, cancel_date FROM dividend_pending WHERE product=?", conn, params=[product])
    for c in ["plan_date", "sgm_date", "impl_date", "report_date", "cancel_date"]:
        pe[c] = pd.to_datetime(pe[c], errors="coerce")
    pe["ex_date"] = pd.NaT
    pe["period_tag"] = pe["report_date"].dt.strftime("%m-%d")
    stage = pe["stage"].fillna("").astype(str)
    pe = pe[~stage.str.contains("|".join(C.PENDING_DEAD_WORDS))]
    stage = pe["stage"].fillna("").astype(str)
    pe["no_div"] = stage.str.contains("|".join(C.PENDING_NO_DIV_WORDS)) | (pe["div_per_share"] == 0)
    return pe.dropna(subset=["plan_date", "period_tag"]).sort_values("plan_date")


def load_dividend_point_index(conn, product):
    return pd.read_sql("SELECT date, value FROM dividend_point_index WHERE product=? ORDER BY date",
                       conn, params=[product])


def truth_increments(dpi: pd.DataFrame) -> pd.Series:
    """股息点指数日增量 = 当天真实分红点数。每年12月重设归零，归零那天的增量取当天值本身"""
    d = dpi.sort_values("date").copy()
    d["inc"] = d["value"].diff()
    reset = d["inc"] < -1
    d.loc[reset, "inc"] = d.loc[reset, "value"]
    return d.dropna(subset=["inc"]).set_index("date")["inc"]


def _le(sorted_dates, d):
    """不晚于 d 的最近一个日期（二分查找）"""
    i = bisect_right(sorted_dates, d)
    return sorted_dates[i - 1] if i else None


def roll_weights(codes, wvals, pb, pt):
    """月末权重按涨跌幅推到目标日（报告 4.1）：w_nt = w_n0(1+r_n) / Σ w_i0(1+r_i)

    分母覆盖【整个指数】，缺价的股票按 ratio=1（不漂移）处理，
    否则只有少数股票有价时它们会被归一化成100%，分红点数放大几十倍。
    返回 (权重% Series 或 None, 有价覆盖率)
    """
    ok = np.isfinite(pb) & np.isfinite(pt) & (pb > 0) & (pt > 0)
    tot = np.nansum(wvals)
    cover = float(wvals[ok].sum() / tot) if tot > 0 else 0.0
    ratio = np.where(ok, pt / np.where(ok, pb, 1.0), 1.0)
    num = wvals * ratio
    den = np.nansum(num)
    if den <= 0:
        return None, cover
    return pd.Series(num / den * 100.0, index=codes), cover


class Panel:
    """某品种预测所需的全部数据，按日期建好索引。建一次，反复查询"""

    def __init__(self, conn, product, positive_only=False):
        self.product = product
        self.ev = load_events(conn, product, positive_only)
        px = pd.read_sql("SELECT date, stock_code, close FROM constituent_daily WHERE product=?",
                         conn, params=[product])
        w = pd.read_sql("SELECT date, stock_code, weight FROM index_weight WHERE product=?",
                        conn, params=[product])
        idx = pd.read_sql("SELECT date, close FROM index_daily WHERE product=? ORDER BY date",
                          conn, params=[product])
        self.n_px, self.n_w = len(px), len(w)

        self.ev_by_code = {c: g.sort_values("ex_date").reset_index(drop=True) for c, g in self.ev.groupby("stock_code")}
        self._ex_list = {c: g["ex_date"].tolist() for c, g in self.ev_by_code.items()}
        self._hcache = {}
        self.pending = load_pending(conn, product)
        self.pending_by_code = {c: g for c, g in self.pending.groupby("stock_code")}
        self.px_by_date = {d: g.set_index("stock_code")["close"] for d, g in px.groupby("date")}
        self.w_by_date = {d: (g["stock_code"].values, g["weight"].values.astype(float))
                          for d, g in w.groupby("date")}
        self.px_dates = sorted(self.px_by_date)
        self.w_dates = sorted(self.w_by_date)
        self.idx_dates = idx["date"].tolist()
        self.idx_close = dict(zip(idx["date"], idx["close"]))
        self._base_px = {}
        fin = pd.read_sql("SELECT stock_code, report_date, pub_date, np_parent FROM stock_financials"
                          " WHERE np_parent IS NOT NULL AND pub_date IS NOT NULL", conn)
        fin = fin[fin["stock_code"].isin(set(self.ev["stock_code"]))]
        fin["report_date"] = pd.to_datetime(fin["report_date"], errors="coerce")
        fin["pub_date"] = pd.to_datetime(fin["pub_date"], errors="coerce")
        fin = fin.dropna(subset=["report_date", "pub_date"]).sort_values("pub_date")
        self.fin_by_code = {c: (g["pub_date"].tolist(),
                                [(d.year, d.strftime("%m-%d")) for d in g["report_date"]],
                                g["np_parent"].astype(float).tolist())
                            for c, g in fin.groupby("stock_code")}
        evs = self.ev.sort_values("ex_date")
        self._ev_ex = evs["ex_date"].tolist()
        self._gap_plan = evs["gap_plan"].to_numpy(dtype=float)
        self._gap_sgm = evs["gap_sgm"].to_numpy(dtype=float)

    @property
    def empty(self):
        return self.ev.empty or not self.px_dates or not self.w_dates or not self.idx_dates

    def base_weights(self, asof):
        """不晚于 asof 的最近一期月末权重 → (基期日, codes, weights)"""
        b = _le(self.w_dates, asof)
        return (b, *self.w_by_date[b]) if b else (None, None, None)

    def weights_at(self, asof, min_cover=0.0, px_exact=False):
        """T日权重（月末权重外推）。返回 (weights% Series 或 None, 覆盖率)

        px_exact=True 时目标日价格必须正好是 asof（对账用）；否则取不晚于 asof 的最近价。
        覆盖率低于 min_cover、或缺价格无法外推时，退回未外推的基期权重。
        """
        b, codes, wvals = self.base_weights(asof)
        if b is None:
            return None, 0.0
        raw = pd.Series(wvals, index=codes)
        cb = _le(self.px_dates, b)
        ct = asof if px_exact else _le(self.px_dates, asof)
        if cb is None or ct not in self.px_by_date:
            return raw, 0.0
        pb = self._base_px.get((b, cb))
        if pb is None:
            pb = self._base_px[(b, cb)] = self.px_by_date[cb].reindex(codes).values
        pt = self.px_by_date[ct].reindex(codes).values
        wt, cover = roll_weights(codes, wvals, pb, pt)
        if wt is None or cover < min_cover:
            return raw, cover
        return wt, cover

    def market_gaps(self, asof):
        """asof 之前已除息事件的 预案/决案→除息 间隔中位数（point-in-time）"""
        i = bisect_right(self._ev_ex, pd.Timestamp(asof))
        if i == 0:
            return None
        return {"gap_plan": float(np.nanmedian(self._gap_plan[:i])) if np.isfinite(self._gap_plan[:i]).any() else np.nan,
                "gap_sgm": float(np.nanmedian(self._gap_sgm[:i])) if np.isfinite(self._gap_sgm[:i]).any() else np.nan}

    def financials_at(self, code, asof):
        """asof 当时已披露的财务 {(年, 'MM-DD'): 归母净利润}（按 pub_date，point-in-time）"""
        f = self.fin_by_code.get(code)
        if f is None:
            return {}
        n = bisect_right(f[0], pd.Timestamp(asof))
        return dict(zip(f[1][:n], f[2][:n]))

    def history(self, code, asof, cutoff):
        """该股 asof 当日（含）之前已除息的历史，及其派生量。

        这些量只在该股发生新的除息时才变，所以按「已除息笔数」缓存，逐日预计算时不必每天重算
        """
        g, ex = self.ev_by_code[code], self._ex_list[code]
        n = bisect_right(ex, asof)
        h = self._hcache.get((code, n))
        if h is None:
            past = g.iloc[:n]
            na = past[past["period_tag"] != "12-31"]
            h = self._hcache[(code, n)] = {
                "past": past,
                "by_tag": {t: x for t, x in past.groupby("period_tag")},
                "other": na.groupby(na["report_date"].dt.year)["div_per_share"].sum().to_dict(),
                "tags": {}, "dps": {}, "fy": {}}
        # 门槛：近 lookback 年内各报告期出现次数（cutoff 每天变，按其在序列中的位置缓存）
        i = bisect_left(ex, cutoff, 0, n)
        if i not in h["tags"]:
            h["tags"][i] = g["period_tag"].iloc[i:n].value_counts().to_dict()
        return h, h["tags"][i]

    def prices_at(self, asof):
        ct = _le(self.px_dates, asof)
        return self.px_by_date[ct] if ct else None

    def index_at(self, asof):
        d = _le(self.idx_dates, asof)
        return float(self.idx_close[d]) if d else np.nan


# ================================================================== 预测

def predict_stocks(panel: Panel, asof, params: Params = Params(), detail=False):
    """T日各成分股各报告期的 (预测除息日, 贡献点数)，不限窗口。

    与合约位次/预测窗口无关，一天只需算一次，各位次共用。
    detail=True 时返回带依据的明细 dict，否则返回 (ex_date, pts) 元组，快。
    """
    a_str = str(pd.Timestamp(asof).date())
    a = pd.Timestamp(a_str)
    wt, _ = panel.weights_at(a_str)
    prices = panel.prices_at(a_str)
    S = panel.index_at(a_str)
    if wt is None or prices is None or not np.isfinite(S):
        return None
    cutoff = a - pd.Timedelta(days=365 * params.lookback_years)
    mgaps = panel.market_gaps(a)

    out = []
    for code, wgt in wt.items():
        if not np.isfinite(wgt) or wgt <= 0:
            continue
        p = prices.get(code)
        if p is None or not np.isfinite(p) or p <= 0:
            continue
        g = panel.ev_by_code.get(code)
        pg = panel.pending_by_code.get(code)
        if g is None and pg is None:
            continue
        # 当日除息的已体现在当日收盘价里，算作"过去"，不再计入未来分红
        # （与已实现分红 div_points = 股息点(到期) − 股息点(当日) 口径一致）
        if g is None:
            g = panel.ev.iloc[:0]
            h, cnt = {"past": g, "by_tag": {}, "other": {}, "tags": {}, "dps": {}, "fy": {}}, {}
        else:
            h, cnt = panel.history(code, a, cutoff)
        past = h["past"]
        # point-in-time：只能看到 plan_date <= asof、且尚未除息的事件
        fut = g.iloc[len(past):]
        known = fut[fut["plan_date"] <= a]
        # 在途预案（还没定除息日）：预案已发出、且没超过有效期的才采信
        if pg is not None:
            # 取消公告日 <= 当日 才算"当时已知取消"，point-in-time；之前的日子里它仍是有效预案
            pg = pg[(pg["plan_date"] <= a) & (a <= pg["plan_date"] + pd.Timedelta(days=C.PENDING_MAX_DAYS))
                    & ~(pg["cancel_date"] <= a)]
        no_div_tags = set(pg.loc[pg["no_div"], "period_tag"]) if pg is not None and len(pg) else set()

        # 按【报告期】分周期，各预测一笔。年报(12-31)和中报(06-30)是两条独立现金流，
        # 只预测"最近一笔"会系统性漏掉年末的中报/特别分红。
        # 门槛：该报告期近年反复出现才纳入，否则偶发的特别股息会被年年预测（实测高估）。
        # 已公告的那笔不受门槛限制 —— 它是事实，不是预测。
        tags = [t for t, k in cnt.items() if t and k >= params.min_occurrence]
        tags += [t for t in known["period_tag"].dropna().unique() if t and t not in tags]
        if pg is not None:
            tags += [t for t in pg["period_tag"].unique() if t and t not in tags]
        # 年报那笔放最后：它要减去同年度中报/季报（已发、已公告、刚预测的）
        tags.sort(key=lambda t: t == "12-31")
        other_by_fy = dict(h["other"])

        for tag in tags:
            ht = h["by_tag"].get(tag, past.iloc[:0])
            k = known[known["period_tag"] == tag]
            cur = k.iloc[-1].to_dict() if len(k) else None
            if cur is None and tag in no_div_tags:
                continue                                    # 预案明确本期不分红
            if cur is None and pg is not None:
                kp = pg[(pg["period_tag"] == tag) & ~pg["no_div"]]
                cur = kp.iloc[-1].to_dict() if len(kp) else None
            # 这一笔属于哪个会计年度：已公告的看其报告期，否则是该报告期上一笔的下一年
            if cur is not None and pd.notna(cur.get("report_date")):
                fy = pd.Timestamp(cur["report_date"]).year
            else:
                fy = int(ht["report_date"].dt.year.max()) + 1 if len(ht) else None
            if cur is not None and np.isfinite(cur.get("div_per_share") or np.nan):
                dps, dsrc = cur["div_per_share"], "已公告"
            elif ht.empty:
                continue                                    # 无公告金额、也无历史可外推
            elif tag == "12-31" and params.annual_by_fy:
                # 按【全年合计】预测，再减去同年度其他报告期的。开始中期分红的公司年报那笔会变小，
                # 按年报自己的历史外推会把中期那部分重复算一遍（如招行 2025：中报 1.01 + 年报 1.00）
                if fy not in h["fy"]:
                    hist_y = past[past["report_date"].dt.year < fy]
                    h["fy"][fy] = (yearly_dps(hist_y),
                                   hist_y.groupby(hist_y["report_date"].dt.year)["div_total"].sum(min_count=1).to_dict())
                totals, dt_by_y = h["fy"][fy]
                dps, dsrc = extrapolate(totals, params.consecutive)
                if dsrc != "线性外推" and params.profit_path:
                    tot = profit_scaled_total(totals, panel.financials_at(code, a), fy, dt_by_y)
                    if tot is not None:
                        dps, dsrc = tot, "利润×支付率"
                other = other_by_fy.get(fy, 0.0)
                if other > 0 and np.isfinite(dps):
                    dps, dsrc = dps - other, dsrc + "−年内已分"
            else:
                key = (tag, params.consecutive)
                if key not in h["dps"]:
                    h["dps"][key] = predict_dps(ht, params.consecutive)
                dps, dsrc = h["dps"][key]
            if tag != "12-31" and fy is not None and np.isfinite(dps or np.nan) and dps > 0:
                other_by_fy[fy] = other_by_fy.get(fy, 0.0) + dps
            if cur is None:
                cur = {"period_tag": tag, "plan_date": pd.NaT, "sgm_date": pd.NaT,
                       "impl_date": pd.NaT, "ex_date": pd.NaT}
            if not np.isfinite(dps or np.nan) or dps <= 0:
                continue

            ed, esrc = predict_exdate(ht, cur, a, mgaps)
            if ed is None:
                ed, esrc = default_exdate(a), "默认日期"
            # 报告 2.3(5)：实施公告没发，10 天内基本不会除息。预测日落在这 10 天里的：
            #   已出预案 → 顺延到 当日+10 天（它迟早会发生，不能丢）
            #   连预案都没有 → 按报告改用默认日期；默认日期也没有（10/21 后）则同样顺延
            soon = a + pd.Timedelta(days=C.MIN_DAYS_TO_EX)
            if esrc != "已公布" and ed is not None and ed < soon:
                announced = pd.notna(cur.get("plan_date")) and pd.Timestamp(cur["plan_date"]) <= a
                dflt = None if announced else default_exdate(a)
                ed, esrc = (dflt, "默认日期") if dflt is not None and dflt >= soon else (soon, esrc + "·顺延")
            ed = to_friday(ed)
            if ed is None or ed <= a:
                continue
            pts = dps / p * (wgt / 100.0) * S
            if detail:
                out.append({"stock_code": code, "报告期": tag, "ex_date": ed, "dps": dps,
                            "price": p, "weight": wgt, "pts": pts,
                            "日期依据": esrc, "金额依据": dsrc})
            else:
                out.append((ed, pts))
    return out


def forecast(panel: Panel, asof, until, params: Params = Params()):
    """预测 (asof, until] 区间的累计分红点数 → (点数, 明细)。当日除息的已在当日收盘价里"""
    rows = predict_stocks(panel, asof, params, detail=True)
    if rows is None:
        return np.nan, pd.DataFrame()
    a, u = pd.Timestamp(asof), pd.Timestamp(until)
    det = pd.DataFrame([r for r in rows if a < r["ex_date"] <= u])
    return (float(det["pts"].sum()) if len(det) else 0.0), det


def backtest(panel: Panel, dpi, params: Params = Params(), horizons=(30, 60, 90)):
    """在历史各时点做预测，与股息点指数的真实区间增量比对。
    时点：数据起点 1200 天后，每逢单数月前 3 天"""
    truth = truth_increments(dpi)
    truth.index = pd.to_datetime(truth.index)

    dates = pd.to_datetime(panel.idx_dates)
    dates = dates[dates >= dates.min() + pd.Timedelta(days=1200)]
    asofs = [t for t in dates if t.day <= 3 and t.month in (1, 3, 5, 7, 9, 11)]

    out = []
    for t in asofs:
        preds = predict_stocks(panel, t, params)
        if preds is None:
            continue
        for h in horizons:
            u = t + pd.Timedelta(days=h)
            if u > dates.max():
                continue
            pred = float(sum(pts for ed, pts in preds if ed <= u))
            real = float(truth[(truth.index > t) & (truth.index <= u)].sum())
            out.append({"asof": t.date(), "horizon": h, "pred": pred,
                        "real": real, "err": pred - real})
    return pd.DataFrame(out)


def precompute(conn, product, params: Params = Params(), verbose=True) -> int:
    """逐交易日 × 合约位次预测分红点数，写入 dividend_forecast_daily"""
    panel = Panel(conn, product)
    if panel.empty:
        print(f"  [{product}] 数据不全，跳过")
        return 0
    fut = pd.read_sql("SELECT date, contract, last_trading_date FROM futures_daily WHERE product=?",
                      conn, params=[product])
    if fut.empty:
        print(f"  [{product}] 无期货数据，跳过")
        return 0
    fut["last_trading_date"] = pd.to_datetime(fut["last_trading_date"], errors="coerce")
    exp_by_date = {d: g.set_index("contract")["last_trading_date"].to_dict()
                   for d, g in fut.groupby("date")}
    # 有 ETF 期权的品种，另存一份到【同月 ETF 期权到期日】（第4个周三）的窗口，slot 记作 IF00E
    has_etf = bool(C.PRODUCTS[product].get("etf"))
    cal = pd.DatetimeIndex(pd.to_datetime(pd.read_sql("SELECT date FROM trading_calendar ORDER BY date", conn)["date"]))
    etf_exp = {}
    dates = sorted(set(panel.idx_dates) & set(exp_by_date))

    rows, t0 = [], time.time()
    for i, d in enumerate(dates):
        preds = predict_stocks(panel, d, params)          # 每天只算一次，四个位次共用
        if preds is None:
            continue
        a = pd.Timestamp(d)
        for slot, exp in exp_by_date[d].items():
            if pd.isna(exp):
                continue
            sel = [pts for ed, pts in preds if a < ed <= exp]
            rows.append((product, d, str(slot), float(np.nansum(sel)) if sel else 0.0, len(sel)))
            if has_etf:
                key = (exp.year, exp.month)
                if key not in etf_exp:
                    etf_exp[key] = etf_option_expiry(*key, cal)
                oe = etf_exp[key]
                if oe is not None and oe >= a:
                    sel = [pts for ed, pts in preds if a < ed <= oe]
                    rows.append((product, d, f"{slot}E", float(np.nansum(sel)) if sel else 0.0, len(sel)))
        if verbose and (i + 1) % 200 == 0:
            el = time.time() - t0
            print(f"    {i+1}/{len(dates)} 天  用时{el:.0f}s  预计剩余{el/(i+1)*(len(dates)-i-1):.0f}s")

    conn.executemany("INSERT OR REPLACE INTO dividend_forecast_daily"
                     " (product,date,slot,pred_points,n_stock) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def summarize_backtest(r: pd.DataFrame) -> pd.DataFrame:
    return r.groupby("horizon").agg(样本=("err", "size"),
                                    误差中位=("err", "median"),
                                    误差均值=("err", "mean"),
                                    绝对误差中位=("err", lambda x: x.abs().median()),
                                    标准差=("err", "std")).round(3)


SWEEP_COMBOS = [(3, 2), (2, 2), (5, 2), (3, 1), (2, 1), (4, 2), (5, 3)]


def sweep(panel, dpi, combos=SWEEP_COMBOS):
    """扫描 回溯年/最少次数 组合。挑选原则：先看90d误差均值是否贴近0，再选≤3点占比最高"""
    rows = []
    for lb, mo in combos:
        r = backtest(panel, dpi, Params(lb, mo))
        if r.empty:
            continue
        g = r[r["horizon"] == 90]
        rows.append({"回溯年": lb, "最少次数": mo,
                     "90d误差均值": round(g["err"].mean(), 3),
                     "90d误差中位": round(g["err"].median(), 3),
                     "90d绝对中位": round(g["err"].abs().median(), 3),
                     "全窗绝对中位": round(r["err"].abs().median(), 3),
                     "≤3点占比%": round((r["err"].abs() <= 3).mean() * 100, 1)})
    return pd.DataFrame(rows)
