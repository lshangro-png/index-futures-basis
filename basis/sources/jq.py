"""
sources/jq.py —— 用聚宽(JQData)拉取分红相关数据，全部幂等落库

聚宽有查询额度限制：拉一次存本地，之后所有分析只读 SQLite，不再碰聚宽。

    python -m basis jq-probe                    # 验证登录 + 确认字段名
    python -m basis jq-backfill                 # 增量更新（可加 --products 300,500）
    python -m basis jq-backfill --full          # 忽略水位从头重取（补新增字段时用）

凭据：环境变量 JQ_ACCOUNT / JQ_PASS。不落盘、不写入任何文件。

取数内容
--------
1. 指数成分股名单（月末抽样，point-in-time）→ 圈定分红事件范围
2. 分红事件（finance.STK_XR_XD，含预案/股东大会/实施进度）→ dividend_announcement
3. 月末 + 除息日 成分股不复权价格网格           → constituent_daily
4. 指数成分股月末权重（get_index_weights）      → index_weight
5. 归母净利润（含 pub_date）                    → stock_financials
"""

import os
import sys
import time
from datetime import datetime

import pandas as pd
import numpy as np

from .. import db
from ..config import PRODUCTS, parse_products

jq = finance = query = None

# 增量更新时往回重取的天数：覆盖「已入库但当时还没走完流程」的分红事件
# （实施公告日/除息日后补），也覆盖新纳入指数的股票
INCREMENTAL_LOOKBACK_DAYS = 400


def _import_jq():
    global jq, finance, query
    if jq is None:
        try:
            import jqdatasdk
            from jqdatasdk import finance as _f, query as _q
        except ImportError:
            sys.exit("需要 jqdatasdk：pip install jqdatasdk")
        jq, finance, query = jqdatasdk, _f, _q


# ------------------------------------------------------------ 代码换算

# 项目用 600519.SH / 000300.SH，聚宽用 600519.XSHG / 000300.XSHG
def to_jq(code: str) -> str:
    return code.replace(".SH", ".XSHG").replace(".SZ", ".XSHE")


def from_jq(code: str) -> str:
    return code.replace(".XSHG", ".SH").replace(".XSHE", ".SZ")


def auth():
    """登录聚宽。凭据只从环境变量读"""
    _import_jq()
    acc = os.environ.get("JQ_ACCOUNT")
    pwd = os.environ.get("JQ_PASS")
    if not acc or not pwd:
        sys.exit("缺少凭据：请先设置环境变量 JQ_ACCOUNT 和 JQ_PASS\n"
                 "  export JQ_ACCOUNT='你的账号'; export JQ_PASS='你的密码'")
    jq.auth(acc, pwd)
    try:
        q = jq.get_query_count()
        print(f"  ✓ 登录成功，剩余查询额度: {q}")
    except Exception:
        print("  ✓ 登录成功")


# ------------------------------------------------------------ STK_XR_XD 字段适配

# 聚宽各版本字段名可能有差异，候选列表逐个试，probe 会打印真实列名
# 已用真实账号probe确认：现金分红字段是 bonus_ratio_rmb，口径为"每10股派X元(含税)"
# 以下列名已用真实账号 probe 确认（STK_XR_XD 共47列）
EXDATE_CANDIDATES = ["a_xr_date", "xr_date", "ex_date"]
CASHDIV_CANDIDATES = ["bonus_ratio_rmb", "per_cash_div", "dividend_amount",
                      "cash_dividend", "per_dividend", "bonus_cash"]
STAGE_CANDIDATES = ["plan_progress", "progress"]
PLAN_DATE_CANDIDATES = ["board_plan_pub_date", "board_plan_dividend_pubdate",
                        "plan_pub_date"]
SGM_DATE_CANDIDATES = ["shareholders_plan_pub_date"]      # 股东大会公告日
IMPL_DATE_CANDIDATES = ["implementation_pub_date"]        # 分红实施公告日
REG_DATE_CANDIDATES = ["a_registration_date"]             # 股权登记日
REPORT_DATE_CANDIDATES = ["report_date"]                  # 报告期
CANCEL_DATE_CANDIDATES = ["bonus_cancel_pub_date"]        # 取消分红公告日
# 分配股本基数（万股）。实施口径优先，其次股东大会、董事会
SHARE_BASE_CANDIDATES = ["distributed_share_base_implement",
                         "distributed_share_base_shareholders",
                         "distributed_share_base_board"]
DIV_TOTAL_CANDIDATES = ["bonus_amount_rmb"]               # 分红总额(元)

def _pick_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def fetch_dividends(codes_jq, start, end, pending_since=None):
    """STK_XR_XD 分红送股事件。按批查询 → (有除息日的事件, 尚无除息日的在途预案)

    pending_since：在途预案只保留预案日不早于它的，太老的多半是早已停止实施、源里没更新状态。

    只保留现金分红(div_per_share>0)，送转股对指数点位的影响不在这里处理。

    注意 run_query 单次最多返回5000行：300只/批时全历史事件约1.2万行，
    会被静默截断（茅台等排在后面的股票整段丢失，已实测复现）。
    因此批缩到50只/批，且每批内用 offset 分页循环取到取空为止。
    """
    frames, B, PAGE = [], 50, 5000
    for i in range(0, len(codes_jq), B):
        chunk = codes_jq[i:i + B]
        off, rows = 0, 0
        while True:
            q = (query(finance.STK_XR_XD)
                 .filter(finance.STK_XR_XD.code.in_(chunk))
                 .order_by(finance.STK_XR_XD.id)
                 .limit(PAGE).offset(off))
            try:
                df = finance.run_query(q)
            except Exception:
                # 个别版本 run_query 不在 finance 下，退回顶层
                df = jq.get_fundamentals(q)
            if df is None or df.empty:
                break
            frames.append(df)
            rows += len(df)
            if len(df) < PAGE:
                break
            off += PAGE
            print(f"    ℹ 批次{i//B} 超{PAGE}行，继续翻页(offset={off})")
            time.sleep(0.2)
        if rows >= PAGE:
            print(f"    ⚠ 批次{i//B} 取满{rows}行，如仍有截断请缩小批次")
        time.sleep(0.2)   # 控制频率，别把额度瞬间打满
    if not frames:
        return pd.DataFrame(), pd.DataFrame()
    frames = [f for f in frames if len(f) and not f.isna().all().all()]
    if not frames:
        return pd.DataFrame(), pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)

    c_code = "code" if "code" in d.columns else _pick_col(d, ["stock_code", "thscode"])
    c_ex = _pick_col(d, EXDATE_CANDIDATES)
    c_div = _pick_col(d, CASHDIV_CANDIDATES)
    if c_code is None or c_ex is None:
        print(f"    ⚠ STK_XR_XD 缺关键列(code/除息日)，现有列: {list(d.columns)}")
        return pd.DataFrame(), pd.DataFrame()

    out = pd.DataFrame({
        "stock_code": d[c_code].astype(str).map(from_jq),
        "ex_date": pd.to_datetime(d[c_ex], errors="coerce").dt.strftime("%Y-%m-%d"),
    })
    # 每股分红。多数版本口径是"每10股派X元"，probe 确认后再定折算系数
    if c_div is not None:
        out["div_per_share"] = pd.to_numeric(d[c_div], errors="coerce") / 10.0
    else:
        print(f"    ⚠ 没找到现金分红列，候选{CASHDIV_CANDIDATES}都不在: {list(d.columns)}")
        out["div_per_share"] = np.nan
    c_stage = _pick_col(d, STAGE_CANDIDATES)
    out["stage"] = d[c_stage].astype(str).replace({"nan": None, "None": None}) if c_stage else None

    # 四个公告日：预案→股东大会→实施→登记。空值不是缺数据，是「当时还没走到这步」，
    # 德邦报告的除息日预测正是按走到哪一步分档，所以原样保留、不要 dropna
    for col, cands in [("plan_date", PLAN_DATE_CANDIDATES),
                       ("sgm_date", SGM_DATE_CANDIDATES),
                       ("impl_date", IMPL_DATE_CANDIDATES),
                       ("reg_date", REG_DATE_CANDIDATES),
                       ("report_date", REPORT_DATE_CANDIDATES),
                       ("cancel_date", CANCEL_DATE_CANDIDATES)]:
        c = _pick_col(d, cands)
        out[col] = (pd.to_datetime(d[c], errors="coerce").dt.strftime("%Y-%m-%d")
                    if c else None)

    # 分配股本基数：源单位是【万股】，×1e4 换成股，好和每股分红直接相乘
    c_sb = _pick_col(d, SHARE_BASE_CANDIDATES)
    out["shares"] = pd.to_numeric(d[c_sb], errors="coerce") * 1e4 if c_sb else np.nan
    # 分红总额：优先用源字段；缺失时用 每股分红×股本 兜底
    # bonus_amount_rmb 源单位是【万元】（已用茅台验证：3,503,257万 = 350.3亿，
    # 与 每股28.02×12.5亿股 吻合），×1e4 换成元
    c_dt = _pick_col(d, DIV_TOTAL_CANDIDATES)
    out["div_total"] = pd.to_numeric(d[c_dt], errors="coerce") * 1e4 if c_dt else np.nan
    out["div_total"] = out["div_total"].fillna(out["div_per_share"] * out["shares"])

    # 已出预案、还没定除息日：单独返回，存 dividend_pending（主表主键含 ex_date，放不进去）
    pend = out[out["ex_date"].isna() & out["plan_date"].notna() & out["report_date"].notna()]
    if pending_since:
        pend = pend[pend["plan_date"] >= pending_since]
    if len(pend):
        keys = ["stock_code", "report_date", "plan_date"]
        pend = (pend.groupby(keys, as_index=False)["div_per_share"].sum(min_count=1)
                .merge(pend.groupby(keys, as_index=False)[["stage", "sgm_date", "impl_date", "cancel_date"]].first(), on=keys))

    out = out.dropna(subset=["ex_date"])
    out = out[(out["ex_date"] >= start) & (out["ex_date"] <= end)]
    # 同一只股票、同一除息日可能有【多笔】分红（普通股息 + 特别股息，
    # 或年报与特别分红撞在一天）。表主键是 (product, stock_code, ex_date)，
    # 直接 drop_duplicates 会丢掉其中一笔 —— 中国平安2018-06-07 就是这样：
    # 末期股息1.00 + 30周年特别股息0.20，只留下0.20，上证50当天少算6.25点。
    # 正确做法是按金额【求和】，日期类字段取最早（最先公告的那笔）。
    num = out.groupby(["stock_code", "ex_date"], as_index=False)[
        ["div_per_share", "div_total"]].sum(min_count=1)
    other = [c for c in out.columns if c not in
             ("stock_code", "ex_date", "div_per_share", "div_total")]
    first = out.sort_values("plan_date").groupby(
        ["stock_code", "ex_date"], as_index=False)[other].first()
    merged = num.merge(first, on=["stock_code", "ex_date"], how="left")
    n_dup = len(out) - len(merged)
    if n_dup:
        print(f"    合并同除息日多笔分红 {n_dup} 组（普通+特别股息等）")
    return merged, pend


# ------------------------------------------------------------ 其余三件套

def sample_dates(start, end, months=tuple(range(1, 13))):
    """月末抽样日期。

    原为季频(3,6,9,12)，实测除息日距最近季度末权重中位数达50天，
    A股分红又集中在5-7月，季频权重系统性过期。改月频后中位数降到16天，
    成本约46万行（占2亿额度0.23%），值得。
    """
    dates = []
    for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        for m in months:
            d = pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)
            if pd.Timestamp(start) <= d <= pd.Timestamp(end):
                dates.append(d.strftime("%Y-%m-%d"))
    return dates


def fetch_weights(product, dates) -> pd.DataFrame:
    """get_index_weights 时点权重。中证系指数官方支持；
    上证50(000016)若取不到，回退用股本×价格自建市值权重"""
    idx = to_jq(PRODUCTS[product]["index"])
    frames = []
    for d in dates:
        try:
            w = jq.get_index_weights(idx, d)
        except Exception as e:
            print(f"    ⚠ [{product}] {d} 权重查询失败: {e}")
            continue
        if w is None or w.empty:
            continue
        frames.append(pd.DataFrame({
            "product": product, "date": d,
            "stock_code": [from_jq(c) for c in w.index],
            "weight": pd.to_numeric(w["weight"], errors="coerce"),
        }))
        time.sleep(0.1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).dropna(subset=["weight"])


def fetch_financials(codes_jq, batch=50) -> pd.DataFrame:
    """归母净利润。用于德邦报告「路B」：分红总额 = 净利润 × 股利支付率

    关键是同时取 pub_date（财报披露日）。做point-in-time预测时必须按
    pub_date 过滤而不是 report_date —— 2023年报的report_date是2023-12-31，
    但要到2024年4月才公开，用report_date过滤会引入前视偏差。
    """
    frames, PAGE = [], 5000
    for i in range(0, len(codes_jq), batch):
        chunk = codes_jq[i:i + batch]
        off = 0
        while True:
            q = (query(finance.STK_INCOME_STATEMENT)
                 .filter(finance.STK_INCOME_STATEMENT.code.in_(chunk))
                 .order_by(finance.STK_INCOME_STATEMENT.id)
                 .limit(PAGE).offset(off))
            try:
                df = finance.run_query(q)
            except Exception as e:
                print(f"    ⚠ 财务批次{i//batch}失败: {e}")
                break
            if df is None or df.empty:
                break
            keep = [c for c in ["code", "report_date", "pub_date",
                                "np_parent_company_owners", "net_profit"] if c in df.columns]
            frames.append(df[keep])
            if len(df) < PAGE:
                break
            off += PAGE
            time.sleep(0.2)
        time.sleep(0.2)
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "stock_code": d["code"].astype(str).map(from_jq),
        "report_date": pd.to_datetime(d["report_date"], errors="coerce").dt.strftime("%Y-%m-%d"),
        "pub_date": pd.to_datetime(d.get("pub_date"), errors="coerce").dt.strftime("%Y-%m-%d"),
        "np_parent": pd.to_numeric(d.get("np_parent_company_owners"), errors="coerce"),
        "net_profit": pd.to_numeric(d.get("net_profit"), errors="coerce"),
    }).dropna(subset=["report_date"])
    return out.drop_duplicates(["stock_code", "report_date"], keep="last")


def fetch_prices_grid(conn, product, codes, dates, batch_days=1) -> pd.DataFrame:
    """按「月末 + 除息日」这组日期，取成分股不复权收盘价

    只抓这两类日期而不是全日频：权重外推公式
        w_nt = w_n0 × (1+r_n) / Σ w_i0 × (1+r_i)
    只需要 基期(月末) 和 目标日(除息日) 两个时点的价格，中间日子用不到。
    约580万行 vs 全日频1075万行，精度一样，库体积减半。

    fq=None 不复权：算股息率 D/P 要的是除息日当天的原始价。
    库里已有的 (日期,股票) 跳过，可断点续跑。
    """
    if not codes or not dates:
        return pd.DataFrame()

    # 月末很多是周末/节假日（实测199个月末里69个非交易日），而月末正是权重基期。
    # 基期取不到价，r_n = P(除息日)/P(基期) 就算不出来 —— 统一回退到前一个交易日。
    from bisect import bisect_left
    cal = [r[0] for r in conn.execute("SELECT date FROM trading_calendar ORDER BY date")]
    cal_set = set(cal)
    aligned, moved = [], 0
    for d in dates:
        if d in cal_set:
            aligned.append(d)
        else:
            i = bisect_left(cal, d)
            if i:
                aligned.append(cal[i - 1]); moved += 1
    if moved:
        print(f"    [{product}] {moved} 个非交易日已回退到前一交易日")
    dates = sorted(set(aligned))

    # 跳过逻辑：按【该日期已有多少只股票】判断，不能只看"日期存在"。
    # 早期版本只抓除息日，每个日期仅1~2只股票；若仅凭日期存在就跳过，
    # 这些稀疏日期永远补不全，权重外推会因覆盖率不足而失效（实测500覆盖率0%）。
    # 门槛取 codes 的一半：早期未上市的股票本来就取不到，不能要求全有。
    cnt = dict(conn.execute(
        "SELECT date, COUNT(*) FROM constituent_daily WHERE product=? GROUP BY date",
        (product,)))
    need = max(1, int(len(codes) * 0.5))
    todo = [d for d in dates if cnt.get(d, 0) < need]
    thin = sum(1 for d in dates if 0 < cnt.get(d, 0) < need)
    if thin:
        print(f"    [{product}] {thin} 个日期数据过稀（<{need}只），将重新补全")
    if not todo:
        print(f"    [{product}] 价格已齐（{len(dates)}个日期），跳过")
        return pd.DataFrame()
    print(f"    [{product}] 需取 {len(todo)}/{len(dates)} 个日期 × {len(codes)} 只")

    frames, done = [], 0
    for d in todo:
        try:
            px = jq.get_price(codes, start_date=d, end_date=d, frequency="daily",
                              fields=["close"], fq=None, panel=False)
        except Exception as e:
            print(f"      ⚠ {d} 失败: {e}")
            continue
        if px is None or px.empty:
            continue
        px = px.reset_index()
        cc = "code" if "code" in px.columns else px.columns[1]
        frames.append(pd.DataFrame({
            "product": product, "date": d,
            "stock_code": px[cc].astype(str).map(from_jq),
            "close": pd.to_numeric(px["close"], errors="coerce"),
        }))
        done += 1
        if done % 100 == 0:
            # 分批落库，中途断了不用从头再来
            part = pd.concat(frames, ignore_index=True).dropna(subset=["close"])
            db.upsert(conn, "constituent_daily", part)
            print(f"      … 已完成 {done}/{len(todo)} 个日期，落库 {len(part)} 行")
            frames = []
        time.sleep(0.05)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).dropna(subset=["close"])


# ------------------------------------------------------------ 命令

def cmd_probe(args):
    auth()
    S = args.start or "2023-01-01"
    E = args.end or "2023-12-31"

    print("\n【1】STK_XR_XD 分红表（贵州茅台样例）")
    try:
        q = query(finance.STK_XR_XD).filter(finance.STK_XR_XD.code == "600519.XSHG")
        try:
            df = finance.run_query(q)
        except Exception:
            df = jq.get_fundamentals(q)
        print(f"  全部列名: {list(df.columns)}")
        print(df.tail(3).to_string())
    except Exception as e:
        print(f"  ✗ 查询失败: {e}")

    print("\n【2】历史时点成分股 get_index_stocks")
    for p in ["300", "1000"]:
        try:
            codes = jq.get_index_stocks(to_jq(PRODUCTS[p]["index"]), "2023-06-30")
            print(f"  {p}: {len(codes)} 只，示例 {codes[:3]}")
        except Exception as e:
            print(f"  {p}: ✗ {e}")

    print("\n【3】时点权重 get_index_weights")
    for p in PRODUCTS:
        try:
            w = jq.get_index_weights(to_jq(PRODUCTS[p]["index"]), "2023-06-30")
            print(f"  {p}: {len(w) if w is not None else 0} 行"
                  + (f"，列 {list(w.columns)}" if w is not None and len(w) else ""))
        except Exception as e:
            print(f"  {p}: ✗ {e}")

    print("\n【4】估值表 valuation（总股本 capitalization）")
    try:
        v = jq.get_valuation("600519.XSHG", "2023-06-30", "2023-06-30",
                             fields=["capitalization", "circulating_cap"])
        print(v.to_string())
    except Exception as e:
        print(f"  ✗ {e}")

    print("\n探测结束。把上面的列名与字段口径(尤其是每10股还是每股)确认后，"
          "再跑 backfill")


def cmd_backfill(args):
    conn = db.connect(args.db)
    auth()
    today = datetime.now().strftime("%Y-%m-%d")
    end = args.end or today
    prods = parse_products(args.products)
    dry = args.dry_run

    for p in prods:
        cfg = PRODUCTS[p]
        if args.start or args.full:
            start = args.start or "2010-01-01"
        else:
            # 增量：从水位往回退一段重取。不能直接用 MAX(ex_date) 的次日做起点 ——
            # 库里存着已公告的【未来】除息日，水位会跑到今天之后，导致
            # ① 水位之前才补全实施公告日的事件永远不会更新；② 新公告的、除息日早于水位的事件被漏掉
            wm = min(db.last_date(conn, "dividend_announcement", p) or "2010-01-01", end)
            start = max("2010-01-01", (pd.Timestamp(wm) - pd.Timedelta(days=INCREMENTAL_LOOKBACK_DAYS))
                        .strftime("%Y-%m-%d"))
        print(f"\n[{cfg['label']} {p}] {start} ~ {end}")
        if start > end:
            print("  已最新，跳过"); continue

        dates = sample_dates(start, end)
        if dry:
            print(f"  dry-run: 成分股/权重抽样 {len(dates)} 个月末"); continue

        # 1) 成分股名单（圈定分红事件范围）
        unions = {}
        for d in dates:
            try:
                unions[d] = [from_jq(c) for c in jq.get_index_stocks(to_jq(cfg["index"]), d)]
            except Exception as e:
                print(f"  ⚠ {d} 成分股失败: {e}")
            time.sleep(0.1)
        all_codes = sorted({c for v in unions.values() for c in v})
        print(f"  成分股抽样 {len(unions)} 期，去重后 {len(all_codes)} 只")

        # 2) 分红事件。上界放宽一年：已发实施公告、除息日在未来的事件对当下预测最有用
        ev_end = (pd.Timestamp(end) + pd.Timedelta(days=366)).strftime("%Y-%m-%d")
        pending_since = (pd.Timestamp(today) - pd.Timedelta(days=730)).strftime("%Y-%m-%d")
        ev, pend = fetch_dividends([to_jq(c) for c in all_codes], start, ev_end, pending_since)
        # 在途预案是「当前状态」快照：按品种整体替换，已定除息日的自然就移出去了
        conn.execute("DELETE FROM dividend_pending WHERE product=?", (p,))
        if len(pend):
            pend = pend.assign(product=p, snapshot_date=today)
            n = db.upsert(conn, "dividend_pending", pend)
            print(f"  在途预案 → dividend_pending 写入 {n} 行；进度分布: "
                  f"{pend['stage'].value_counts().head(6).to_dict()}")
        conn.commit()
        if ev.empty:
            print("  ⚠ 无分红事件"); continue
        ev["product"] = p
        ev["snapshot_date"] = today
        cols = [c for c in ["product", "stock_code", "ex_date", "div_per_share",
                            "div_total", "shares", "stage", "plan_date", "sgm_date",
                            "impl_date", "reg_date", "report_date", "snapshot_date"]
                if c in ev.columns]
        n = db.upsert(conn, "dividend_announcement", ev[cols])
        print(f"  分红事件 → dividend_announcement 写入 {n} 行")
        for c in ["sgm_date", "impl_date", "div_total"]:
            if c in ev.columns:
                print(f"      {c} 非空率 {ev[c].notna().mean()*100:.1f}%")

        # 3) 月频权重
        w = fetch_weights(p, list(unions.keys()))
        if len(w):
            n = db.upsert(conn, "index_weight", w)
            print(f"  月频权重 → index_weight 写入 {n} 行")
        elif unions:
            print("  ⚠ 权重取空")

        # 4) 价格网格：月末(权重基期) + 已发生的除息日(目标日)
        exd = [d for d in ev["ex_date"].dropna() if d <= end]
        grid = sorted(set(dates) | set(exd))
        px = fetch_prices_grid(conn, p, [to_jq(c) for c in all_codes], grid)
        if len(px):
            n = db.upsert(conn, "constituent_daily", px)
            print(f"  价格网格 → constituent_daily 写入 {n} 行")

    # 5) 财务数据：跨品种共享，按全部出现过的成分股一次取完
    if not dry:
        codes = pd.read_sql(
            "SELECT DISTINCT stock_code FROM dividend_announcement", conn)["stock_code"].tolist()
        if codes:
            print(f"\n[归母净利润] {len(codes)} 只股票")
            fin = fetch_financials([to_jq(c) for c in codes])
            if len(fin):
                n = db.upsert(conn, "stock_financials", fin)
                print(f"  → stock_financials 写入 {n} 行")

    print("\n库内状态:")
    print(db.status(conn).to_string(index=False))
    conn.close()
