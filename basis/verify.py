"""
verify.py —— 数据体检三件套，对应数据可信度的三个层次：

  check      建库后的字段体检 —— 公告日填上没、权重是不是月频、股本单位对不对
  reconcile  分红点数复算对账 —— 用真实已知数据按公式复算，与中证股息点指数比对。
             【不做预测】，验证的是「公式+权重」本身的精度
  diagnose   逐日归因 —— 某天误差大，到底是哪只股票的数据出了问题
"""

import numpy as np
import pandas as pd

from .dividend import Panel, load_dividend_point_index, truth_increments


# ============================================================ check

def check(conn, p):
    print("=" * 66)
    print(f"【1】公告日字段非空率  product={p}")
    d = pd.read_sql("SELECT * FROM dividend_announcement WHERE product=?", conn, params=[p])
    print(f"  总行数 {len(d):,}")
    for col in ["plan_date", "sgm_date", "impl_date", "reg_date", "report_date",
                "div_per_share", "shares", "div_total"]:
        if col in d.columns:
            nn = d[col].notna().sum()
            print(f"    {col:<15} {nn:>7,}  ({nn/max(len(d),1)*100:5.1f}%)")
    print("  判读：sgm_date/impl_date 不会是100%（进行中的分红还没走完流程），")
    print("        但若接近0%，说明字段映射有问题。")

    print("\n" + "=" * 66)
    print(f"【2】权重频率  product={p}")
    w = pd.read_sql(
        "SELECT date, COUNT(*) n, ROUND(SUM(weight),3) sum_w "
        "FROM index_weight WHERE product=? GROUP BY date ORDER BY date", conn, params=[p])
    print(f"  共 {len(w)} 个时点")
    recent = w.tail(14)
    print(recent.to_string(index=False))
    months = pd.to_datetime(w["date"]).dt.month.unique()
    print(f"\n  出现过的月份: {sorted(months)}")
    print(f"  → {'✓ 月频生效' if len(set(months)) > 4 else '✗ 仍是季频，需要用价格网格做权重外推'}")
    if len(recent) >= 2:
        same = recent["sum_w"].nunique() == 1 and recent["n"].nunique() == 1
        print(f"  相邻时点权重合计是否完全相同: {'是（可疑，可能是同一份快照）' if same else '否（正常）'}")

    print("\n" + "=" * 66)
    print("【3】股本单位（以贵州茅台为准，总股本应在 12.5 亿股量级）")
    t = pd.read_sql(
        "SELECT stock_code, ex_date, div_per_share, shares, div_total "
        "FROM dividend_announcement WHERE stock_code LIKE '600519%' "
        "ORDER BY ex_date DESC LIMIT 5", conn)
    if t.empty:
        print("  没找到茅台记录（可能该品种不含它）")
    else:
        print(t.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        s = t["shares"].dropna()
        if len(s):
            v = s.iloc[0]
            if 5e8 < v < 5e9:
                print(f"\n  ✓ shares={v:,.0f} 在 12.5亿股 量级，单位正确")
            elif 5e4 < v < 5e5:
                print(f"\n  ✗ shares={v:,.0f} 看着是【万股】，×1e4 那步没生效")
            else:
                print(f"\n  ⚠ shares={v:,.0f} 量级异常，需人工确认")
        dt = t["div_total"].dropna()
        if len(dt):
            print(f"  div_total 首行 {dt.iloc[0]:,.0f} 元 —— 茅台应在【几百亿】量级")

    print("\n" + "=" * 66)
    print("【附】价格网格进度")
    g = pd.read_sql(
        "SELECT COUNT(*) rows, COUNT(DISTINCT date) dates, MIN(date) a, MAX(date) b "
        "FROM constituent_daily WHERE product=?", conn, params=[p])
    print(g.to_string(index=False))


# ============================================================ reconcile

def reconcile(conn, product, roll=True, min_cover=0.90):
    dpi = load_dividend_point_index(conn, product)
    if dpi.empty:
        print(f"[{product}] 没有股息点指数，无法对账（1000 本来就没有）")
        return None
    panel = Panel(conn, product, positive_only=True)
    ev = panel.ev
    print(f"[{product}] 分红事件 {len(ev):,} | 价格 {panel.n_px:,} | "
          f"权重 {panel.n_w:,} | 股息点指数 {len(dpi):,}")

    out, covers = [], []
    skipped = {"无指数": 0, "无权重基期": 0, "无价格": 0, "外推覆盖不足": 0}
    for ex_ts, g in ev.groupby("ex_date"):
        ex = ex_ts.strftime("%Y-%m-%d")
        S = panel.idx_close.get(ex)
        if S is None or not np.isfinite(S):
            skipped["无指数"] += 1; continue
        pt = panel.px_by_date.get(ex)
        if pt is None:
            skipped["无价格"] += 1; continue
        b, codes, wvals = panel.base_weights(ex)
        if b is None:
            skipped["无权重基期"] += 1; continue

        if roll:
            # 覆盖率不足时 weights_at 退回基期权重
            wt, cover = panel.weights_at(ex, min_cover=min_cover, px_exact=True)
            covers.append(cover)
            if cover < min_cover:
                skipped["外推覆盖不足"] += 1
        else:
            wt = pd.Series(wvals, index=codes)

        sub = g[g["stock_code"].isin(wt.index) & g["stock_code"].isin(pt.index)]
        if sub.empty:
            continue
        yld = sub["div_per_share"].values / pt.reindex(sub["stock_code"]).values
        wgt = wt.reindex(sub["stock_code"]).values / 100.0
        out.append({"date": ex, "pts_calc": np.nansum(yld * wgt) * S, "n_stock": len(sub)})

    if not out:
        print("没有可对账的日期")
        return None
    calc = pd.DataFrame(out).sort_values("date")
    print(f"\n复算了 {len(calc):,} 个除息日；跳过情况: {({k: v for k, v in skipped.items() if v}) or '无'}")
    if covers:
        cv = pd.Series(covers)
        print(f"权重外推覆盖率: 中位 {cv.median()*100:.1f}%  最低 {cv.min()*100:.1f}%  "
              f"低于{min_cover:.0%}的天数 {(cv < min_cover).sum()}")

    truth = truth_increments(dpi).rename("pts_true").reset_index()
    m = calc.merge(truth, on="date", how="inner").dropna()
    if m.empty:
        print("与股息点指数没有可比日期")
        return None
    m["err"] = m["pts_calc"] - m["pts_true"]

    print("\n" + "=" * 68)
    print(f"单日分红点数误差（复算 − 真值），共 {len(m):,} 天")
    print(m["err"].describe().round(3).to_string())
    print(f"\n绝对误差中位数: {m['err'].abs().median():.4f} 点")
    print(f"相关性: {m['pts_calc'].corr(m['pts_true']):.6f}")

    print("\n分年度（德邦报告的基准是全年累计误差 ±10 点以内）")
    # 真值按【全年所有日子】统计 —— 只算匹配日会系统性低估真值、虚高准确率
    truth_yr = truth.assign(year=truth["date"].str[:4]).groupby("year")["pts_true"].sum()
    calc_yr = calc.assign(year=calc["date"].str[:4]).groupby("year")["pts_calc"].sum()
    yr = pd.DataFrame({"复算累计": calc_yr, "真值累计(全年)": truth_yr}).dropna()
    yr["累计误差"] = (yr["复算累计"] - yr["真值累计(全年)"]).round(2)
    yr["相对误差%"] = (yr["累计误差"] / yr["真值累计(全年)"] * 100).round(1)
    print(yr.round(1).to_string())

    print("\n误差最大的10天:")
    print(m.reindex(m["err"].abs().sort_values(ascending=False).index)
          .head(10).round(3).to_string(index=False))
    return m


# ============================================================ diagnose

def _detail(conn, product, date):
    """某天参与复算的股票明细 + 各自贡献的分红点数（未做权重外推）"""
    ev = pd.read_sql(
        "SELECT stock_code, div_per_share, div_total, shares, plan_date, impl_date "
        "FROM dividend_announcement WHERE product=? AND ex_date=?", conn, params=[product, date])
    if ev.empty:
        return None, "当天库里没有分红记录"
    px = pd.read_sql("SELECT stock_code, close FROM constituent_daily WHERE product=? AND date=?",
                     conn, params=[product, date])
    wd = conn.execute("SELECT MAX(date) FROM index_weight WHERE product=? AND date<=?",
                      (product, date)).fetchone()[0]
    if wd is None:
        return None, "找不到权重基期"
    w = pd.read_sql("SELECT stock_code, weight FROM index_weight WHERE product=? AND date=?",
                    conn, params=[product, wd])
    r = conn.execute("SELECT close FROM index_daily WHERE product=? AND date<=? ORDER BY date DESC LIMIT 1",
                     (product, date)).fetchone()
    if r is None:
        return None, "当天没有指数点位"
    S = float(r[0])
    m = ev.merge(px, on="stock_code", how="left").merge(w, on="stock_code", how="left")
    m["在指数内"] = m["weight"].notna()
    m["pts"] = m["div_per_share"] / m["close"] * (m["weight"] / 100.0) * S
    m["每点对应dps"] = m["close"] / ((m["weight"] / 100.0) * S)   # 该股要独自补1点缺口需加的每股分红
    return m.sort_values("pts", ascending=False), S


def _truth_on(truth, date):
    v = truth.get(date)
    return float(v) if v is not None and pd.notna(v) else None


def report_day(conn, product, date, truth):
    print("=" * 70)
    print(f"【{product}】{date}")
    m, S = _detail(conn, product, date)
    if m is None:
        print("  " + S)
        return
    t = _truth_on(truth, date)
    calc = float(m["pts"].sum(skipna=True))
    print(f"  指数点位 {S:,.2f} | 库中分红 {len(m)} 笔 | 在指数内 {int(m['在指数内'].sum())} 只")
    print(f"  复算 {calc:.3f} 点" + (f" | 真值 {t:.3f} 点 | 差 {calc-t:+.3f}" if t is not None else ""))
    show = ["stock_code", "div_per_share", "close", "weight", "pts", "每点对应dps", "plan_date", "impl_date"]
    print("\n  明细（pts=该股贡献的分红点数）:")
    print(m[show].round(4).to_string(index=False))

    if t is not None and abs(calc - t) > 0.3:
        gap = t - calc
        print(f"\n  ⚠ 缺口 {gap:+.3f} 点。若由单只股票造成，该股每股分红需再加：")
        cand = m[m["在指数内"]].copy()
        cand["需补dps"] = gap * cand["每点对应dps"]
        cand["补后dps"] = cand["div_per_share"] + cand["需补dps"]
        print(cand[["stock_code", "div_per_share", "需补dps", "补后dps"]].round(4).to_string(index=False))
        print("  提示：若某行『补后dps』刚好是个整洁数字（如1.20、0.50），")
        print("        多半是该股当天有【两笔】分红（普通+特别股息）被合并丢了一笔。")


def diagnose(conn, product, date=None, worst=0):
    truth = truth_increments(load_dividend_point_index(conn, product))
    if date:
        report_day(conn, product, date, truth)
        return
    if not worst:
        print("需要 --date 或 --worst")
        return
    ex = [r[0] for r in conn.execute(
        "SELECT DISTINCT ex_date FROM dividend_announcement WHERE product=?", (product,))]
    rows = []
    for d in ex:
        t = _truth_on(truth, d)
        if t is None:
            continue
        m, _ = _detail(conn, product, d)
        if m is not None:
            rows.append({"date": d, "calc": float(m["pts"].sum(skipna=True)), "true": t})
    r = pd.DataFrame(rows)
    r["err"] = r["calc"] - r["true"]
    top = r.reindex(r["err"].abs().sort_values(ascending=False).index).head(worst)
    print(top.round(3).to_string(index=False), "\n")
    for d in top["date"]:
        report_day(conn, product, d, truth)
        print()
