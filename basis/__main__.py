"""
统一命令行入口：python -m basis <命令>

  数据
    init                         从 data/ 下的 CSV/Excel 建库
    status                       各表数据水位
    vacuum                       删冗余索引并压缩数据库
    jq-probe / jq-backfill       聚宽：分红事件、权重、成分股价格、财务

  分红预测
    precompute                   逐日预测分红点数落库（notebook 的紫色预测线依赖它）
    forecast --asof D --to D     单点预测，打印明细
    backtest [--sweep]           历史回测 / 参数扫描

  体检
    check / reconcile / diagnose

  报告
    report                       计算四品种基差，导出 results/ 下的 HTML + CSV
"""

import argparse
import sys

from . import config as C


def _conn(a):
    from . import db
    return db.connect(a.db)


def cmd_init(a):
    from . import db, importers
    conn = _conn(a)
    print(f"数据库: {a.db}\n")
    importers.init_all(conn, a.data)
    print("\n" + "=" * 62)
    print(db.status(conn).to_string(index=False))


def cmd_status(a):
    from . import db
    print(db.status(_conn(a)).to_string(index=False))


def cmd_vacuum(a):
    from . import db
    db.vacuum(a.db)


def cmd_precompute(a):
    from . import dividend as D
    conn = _conn(a)
    params = D.Params(a.lookback, a.min_occ)
    for p in C.parse_products(a.products):
        print(f"[{p}] 预计算中…（参数 回溯{params.lookback_years}年/至少{params.min_occurrence}次）")
        print(f"  → dividend_forecast_daily 写入 {D.precompute(conn, p, params)} 行")


def cmd_forecast(a):
    from . import dividend as D
    panel = D.Panel(_conn(a), a.product)
    pts, det = D.forecast(panel, a.asof, a.to, D.Params(a.lookback, a.min_occ))
    print(f"[{a.product}] {a.asof} → {a.to} 预测累计分红点数: {pts:.3f}")
    if len(det):
        det["ex_date"] = det["ex_date"].dt.date
        print(f"\n涉及 {len(det)} 笔，贡献前10：")
        print(det.nlargest(10, "pts").round(4).to_string(index=False))
        print("\n除息日依据分布：\n" + det["日期依据"].value_counts().to_string())
        print("\n金额依据分布：\n" + det["金额依据"].value_counts().to_string())


def cmd_backtest(a):
    from . import dividend as D
    conn = _conn(a)
    dpi = D.load_dividend_point_index(conn, a.product)
    if dpi.empty:
        print("无股息点指数，无法回测（1000 没有）")
        return
    panel = D.Panel(conn, a.product)
    print(f"[{a.product}] 事件 {len(panel.ev):,} | 价格 {panel.n_px:,} | 权重 {panel.n_w:,}")

    if a.sweep:
        print(f"\n扫描 {len(D.SWEEP_COMBOS)} 组参数（当前默认 {C.LOOKBACK_YEARS}/{C.MIN_OCCURRENCE}）…")
        print(D.sweep(panel, dpi).to_string(index=False))
        print("\n挑选原则：先看【90d误差均值】是否贴近0（不一边倒），再在其中选【≤3点占比%】最高的那组。")
        print("定下来后改 basis/config.py 的 LOOKBACK_YEARS / MIN_OCCURRENCE，重跑 precompute。")
        return

    params = D.Params(a.lookback, a.min_occ)
    print(f"\n回测中（参数 回溯{params.lookback_years}年/至少{params.min_occurrence}次，"
          f"每两月一个时点 × 30/60/90天窗口）…")
    r = D.backtest(panel, dpi, params)
    if r.empty:
        print("无有效回测样本")
        return
    print(f"\n共 {len(r)} 个样本\n\n按窗口长度：")
    print(D.summarize_backtest(r).to_string())
    print(f"\n|误差| <= 3点 的比例: {(r['err'].abs() <= 3).mean() * 100:.1f}%（德邦报告基准：±3 点以内）")
    print("\n误差最大的8个样本：")
    print(r.reindex(r["err"].abs().sort_values(ascending=False).index).head(8).round(3).to_string(index=False))


def cmd_check(a):
    from . import verify
    verify.check(_conn(a), a.product)


def cmd_reconcile(a):
    from . import verify
    verify.reconcile(_conn(a), a.product, roll=not a.no_roll)


def cmd_diagnose(a):
    from . import verify
    verify.diagnose(_conn(a), a.product, a.date, a.worst)


def cmd_report(a):
    from . import calc, report
    conn = _conn(a)
    results = calc.analyze_all(conn, a.start, a.end, C.parse_products(a.products))
    report.render_html(results, a.out)
    report.export_csv(results, a.out)


def cmd_jq_probe(a):
    from .sources import jq
    jq.cmd_probe(a)


def cmd_jq_backfill(a):
    from .sources import jq
    jq.cmd_backfill(a)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows 控制台默认 GBK
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m basis", description="股指基差分析",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<命令>")

    def add(name, func, help, product=False, products=False, params=False):
        p = sub.add_parser(name, help=help)
        p.add_argument("--db", default=C.DB_PATH)
        if product:
            p.add_argument("--product", default="300", choices=list(C.PRODUCTS))
        if products:
            p.add_argument("--products", nargs="*", help="空格或逗号分隔，默认全部")
        if params:
            p.add_argument("--lookback", type=int, default=C.LOOKBACK_YEARS, help="报告期回溯年限")
            p.add_argument("--min-occ", type=int, default=C.MIN_OCCURRENCE, help="该报告期至少出现几次才纳入预测")
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "从 data/ 建库并导入")
    p.add_argument("--data", default=C.DATA_DIR)
    add("status", cmd_status, "查看数据水位")
    add("vacuum", cmd_vacuum, "删冗余索引并压缩数据库")

    add("precompute", cmd_precompute, "逐日预测分红落库", products=True, params=True)
    p = add("forecast", cmd_forecast, "单点分红预测", product=True, params=True)
    p.add_argument("--asof", required=True); p.add_argument("--to", required=True)
    p = add("backtest", cmd_backtest, "分红预测回测", product=True, params=True)
    p.add_argument("--sweep", action="store_true", help="扫描多组参数")

    add("check", cmd_check, "字段体检", product=True)
    p = add("reconcile", cmd_reconcile, "分红点数对账", product=True)
    p.add_argument("--no-roll", action="store_true", help="不做权重外推，作对照")
    p = add("diagnose", cmd_diagnose, "逐日归因", product=True)
    p.add_argument("--date"); p.add_argument("--worst", type=int, default=0)

    p = add("report", cmd_report, "计算基差并导出 HTML/CSV", products=True)
    p.add_argument("--start", default="2022-07-01"); p.add_argument("--end")
    p.add_argument("--out", default=C.RESULTS_DIR)

    p = add("jq-probe", cmd_jq_probe, "聚宽：探测接口字段")
    p.add_argument("--start"); p.add_argument("--end")
    p = add("jq-backfill", cmd_jq_backfill, "聚宽：回填/增量更新", products=True)
    p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--full", action="store_true", help="忽略水位从头重取")

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
