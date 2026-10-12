"""
db.py —— SQLite 存储层：建表、幂等写入、查询接口

所有写入走 INSERT OR REPLACE，主键 (product, date, ...)，
因此重复导入不会产生脏行，回填和增量更新都可以放心重跑。
"""

import sqlite3
from datetime import timedelta
from typing import Optional

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_daily (
    product TEXT, date TEXT, close REAL, PRIMARY KEY (product, date));

-- contract 为连续合约位次(IF00..IF03)，contract_code 为当期实际合约(IF2409)
CREATE TABLE IF NOT EXISTS futures_daily (
    product TEXT, date TEXT, contract TEXT, contract_code TEXT,
    close REAL, settle REAL, open_interest REAL, volume REAL, turnover REAL,
    last_trading_date TEXT, days INTEGER, div_points REAL,
    PRIMARY KEY (product, date, contract));

-- 中证股息点指数，区间差值即已实现分红点数
CREATE TABLE IF NOT EXISTS dividend_point_index (
    product TEXT, date TEXT, value REAL, PRIMARY KEY (product, date));

CREATE TABLE IF NOT EXISTS etf_daily (
    product TEXT, date TEXT, close REAL, PRIMARY KEY (product, date));

CREATE TABLE IF NOT EXISTS option_quotes (
    product TEXT, date TEXT, option_code TEXT, contract_name TEXT,
    close REAL, volume REAL, notional REAL,
    PRIMARY KEY (product, date, option_code));

-- 股指期权(IO/MO/HO)逐合约行情。标的直接是指数，PCP反推不需要ETF换算、
-- 不需要ETF自身分红调整
CREATE TABLE IF NOT EXISTS index_option_quotes (
    product TEXT, date TEXT, option_code TEXT,
    contract_month TEXT, opt_type TEXT, strike REAL,
    close REAL, volume REAL, turnover REAL,
    PRIMARY KEY (product, date, option_code));

CREATE TABLE IF NOT EXISTS shibor (
    date TEXT, tenor TEXT, value REAL, PRIMARY KEY (date, tenor));

CREATE TABLE IF NOT EXISTS trading_calendar (date TEXT PRIMARY KEY);

-- 成分股分红事件（聚宽 STK_XR_XD），同一 (品种,股票,除息日) 唯一。
-- 四个公告日逐事件、point-in-time：
--   plan_date 预案 / sgm_date 股东大会 / impl_date 实施公告 / reg_date 股权登记
-- 后三个可能为空 —— 空本身是有效信息，表示该事件当时还没走到那一步，
-- 德邦报告的除息日预测正是按「走到哪一步」分档的，不要 dropna
CREATE TABLE IF NOT EXISTS dividend_announcement (
    product     TEXT, stock_code TEXT, ex_date TEXT,
    div_per_share REAL, div_total REAL, shares REAL,
    weight REAL, price REAL, stage TEXT,
    plan_date TEXT, sgm_date TEXT, impl_date TEXT, reg_date TEXT,
    report_date TEXT, snapshot_date TEXT,
    PRIMARY KEY (product, stock_code, ex_date));

-- 已出预案、尚未定除息日的分红（聚宽快照）。主表主键含 ex_date 放不进去，单独存。
-- 每次 jq-backfill 按品种整体替换；snapshot_date 为取数日，状态以该日为准
CREATE TABLE IF NOT EXISTS dividend_pending (
    product TEXT, stock_code TEXT, report_date TEXT, plan_date TEXT,
    div_per_share REAL, stage TEXT, sgm_date TEXT, impl_date TEXT, cancel_date TEXT,
    snapshot_date TEXT,
    PRIMARY KEY (product, stock_code, report_date, plan_date));

-- 归母净利润，用于德邦报告「路B」：分红总额 = 净利润 × 股利支付率（尚未实现）。
-- point-in-time 过滤必须用 pub_date 而不是 report_date
CREATE TABLE IF NOT EXISTS stock_financials (
    stock_code TEXT, report_date TEXT, pub_date TEXT,
    np_parent REAL, net_profit REAL,
    PRIMARY KEY (stock_code, report_date));

-- 成分股不复权收盘价，只存「月末 + 除息日」网格（weight 列已弃用，权重看 index_weight）
CREATE TABLE IF NOT EXISTS constituent_daily (
    product TEXT, date TEXT, stock_code TEXT,
    close REAL, weight REAL,
    PRIMARY KEY (product, date, stock_code));

-- 指数成分股月末权重（聚宽 get_index_weights）
CREATE TABLE IF NOT EXISTS index_weight (
    product TEXT, date TEXT, stock_code TEXT, weight REAL,
    PRIMARY KEY (product, date, stock_code));

-- 每个交易日、每个合约位次，预测从当日到该合约到期日之间的累计分红点数。
--   futures_daily.div_points = 事后真值，含前视偏差，不能回测
--   pred_points              = 当日可知信息下的预测值，可用于回测
CREATE TABLE IF NOT EXISTS dividend_forecast_daily (
    product TEXT, date TEXT, slot TEXT,
    pred_points REAL, n_stock INTEGER,
    PRIMARY KEY (product, date, slot));

-- 主键 (product, date, ...) 本身就是索引，按品种/日期查询不需要再建。
-- 只有 stock_financials 按 pub_date 过滤时用得上额外索引
CREATE INDEX IF NOT EXISTS ix_fin ON stock_financials(stock_code, pub_date);
"""

DATE_COL = {"dividend_announcement": "ex_date", "dividend_pending": "plan_date", "stock_financials": "pub_date"}


# ------------------------------------------------------------------ 基础

def connect(path: str = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    _migrate(c)
    return c


def _migrate(conn):
    """给老库补上后加的列。幂等，不动已有数据"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(dividend_announcement)")}
    for col in ["sgm_date", "impl_date", "reg_date", "report_date"]:
        if col not in have:
            conn.execute(f"ALTER TABLE dividend_announcement ADD COLUMN {col} TEXT")
    if "cancel_date" not in {r[1] for r in conn.execute("PRAGMA table_info(dividend_pending)")}:
        conn.execute("ALTER TABLE dividend_pending ADD COLUMN cancel_date TEXT")
    conn.commit()


# 旧版建过、现已冗余的索引（被主键前缀覆盖或无查询使用）与弃用表
_OBSOLETE = ["DROP INDEX IF EXISTS ix_fut", "DROP INDEX IF EXISTS ix_con", "DROP INDEX IF EXISTS ix_con_code",
             "DROP INDEX IF EXISTS ix_iw", "DROP INDEX IF EXISTS ix_opt", "DROP INDEX IF EXISTS ix_iopt",
             "DROP INDEX IF EXISTS ix_fcst", "DROP TABLE IF EXISTS etf_distribution",
             "DROP TABLE IF EXISTS update_log"]


def vacuum(path: str = DB_PATH):
    """删冗余索引/弃用表，再 VACUUM 回收空间。需要约等于库大小的临时磁盘空间"""
    import os
    before = os.path.getsize(path)
    conn = sqlite3.connect(path)
    for sql in _OBSOLETE:
        conn.execute(sql)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.close()
    after = os.path.getsize(path)
    print(f"  {before/2**30:.2f} GB → {after/2**30:.2f} GB")


def to_date_str(x):
    """统一为 YYYY-MM-DD 字符串（标量或 Series）"""
    t = pd.to_datetime(x, errors="coerce")
    if isinstance(t, pd.Series):
        return t.dt.strftime("%Y-%m-%d")
    return None if pd.isna(t) else t.strftime("%Y-%m-%d")


def upsert(conn, table: str, df: pd.DataFrame) -> int:
    """幂等写入。主键冲突整行覆盖，重跑安全。

    注意是【整行】覆盖：df 里没有的列会被写成 NULL。
    """
    if df is None or df.empty:
        return 0
    df = df.dropna(how="all")
    if df.empty:
        return 0
    cols = list(df.columns)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    rows = [tuple(None if pd.isna(v) else v for v in r) for r in df.itertuples(index=False)]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def last_date(conn, table: str, product: str) -> Optional[str]:
    """某表某品种的最新日期，增量更新的起点依据"""
    dc = DATE_COL.get(table, "date")
    try:
        r = conn.execute(f"SELECT MAX({dc}) FROM {table} WHERE product=?", (product,)).fetchone()
        return r[0] if r and r[0] else None
    except sqlite3.OperationalError:
        return None


def next_day(d: Optional[str], default="2010-01-01") -> str:
    return default if not d else (pd.to_datetime(d) + timedelta(days=1)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ 查询

def _q(conn, sql, params, dates=("date",)):
    df = pd.read_sql(sql, conn, params=params)
    for c in dates:
        if c in df.columns and len(df):
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _rng(sql, p, start, end):
    if start:
        sql += " AND date>=?"; p.append(start)
    if end:
        sql += " AND date<=?"; p.append(end)
    return sql + " ORDER BY date", p


def load_index(conn, product, start=None, end=None):
    return _q(conn, *_rng("SELECT date, close AS index_close FROM index_daily WHERE product=?",
                          [product], start, end))


def load_series(conn, table, product, col="close", start=None, end=None):
    return _q(conn, *_rng(f"SELECT date, {col} FROM {table} WHERE product=?",
                          [product], start, end))


def load_shibor(conn, tenor="shibor_3m", start=None, end=None):
    return _q(conn, *_rng("SELECT date, value AS shibor FROM shibor WHERE tenor=?",
                          [tenor], start, end))


def load_options(conn, product, start=None, end=None):
    return _q(conn, *_rng("SELECT date, option_code, contract_name, close, volume, notional"
                          " FROM option_quotes WHERE product=?", [product], start, end))


def load_index_options(conn, product, start=None, end=None):
    return _q(conn, *_rng("SELECT date, option_code, contract_month, opt_type, strike,"
                          " close, volume, turnover FROM index_option_quotes WHERE product=?",
                          [product], start, end))


def load_futures_wide(conn, product, start=None, end=None):
    """四个合约位次摊平成一行一天，列名如 close_00 / days_00"""
    df = _q(conn, *_rng("SELECT * FROM futures_daily WHERE product=?", [product], start, end),
            dates=("date", "last_trading_date"))
    if df.empty:
        return df
    df["slot"] = df["contract"].str[-2:]
    out = None
    keep = ["contract_code", "close", "settle", "open_interest", "volume",
            "turnover", "last_trading_date", "days", "div_points"]
    for slot, g in df.groupby("slot"):
        g = g.set_index("date")[keep]
        g.columns = [f"{c}_{slot}" for c in g.columns]
        out = g if out is None else out.join(g, how="outer")
    return out.reset_index()


def load_forecast_wide(conn, product):
    """dividend_forecast_daily → 一行一天，列名 {slot}_div_pred（期货窗口）/ {slot}_div_pred_etf（ETF期权窗口）"""
    f = pd.read_sql("SELECT date, slot, pred_points FROM dividend_forecast_daily"
                    " WHERE product=?", conn, params=[product])
    if f.empty:
        return f
    # 库里 slot 存的是 'IF00' / 'IF00E' 这种写法，剥成 '00_div_pred' / '00_div_pred_etf'
    m = f["slot"].astype(str).str.extract(r"(\d{2})(E?)$")
    f["col"] = m[0] + "_div_pred" + m[1].map({"E": "_etf", "": ""})
    f = f.dropna(subset=["col"])
    wide = f.pivot(index="date", columns="col", values="pred_points")
    wide.columns = list(wide.columns)
    wide = wide.reset_index()
    wide["date"] = pd.to_datetime(wide["date"], errors="coerce")
    return wide


def status(conn) -> pd.DataFrame:
    rows = []
    for t in ["index_daily", "futures_daily", "dividend_point_index", "etf_daily",
              "option_quotes", "index_option_quotes", "dividend_announcement", "dividend_pending",
              "constituent_daily", "index_weight", "dividend_forecast_daily"]:
        dc = DATE_COL.get(t, "date")
        for _, r in pd.read_sql(
                f"SELECT product, COUNT(*) n, MIN({dc}) a, MAX({dc}) b FROM {t} GROUP BY product",
                conn).iterrows():
            rows.append({"表": t, "品种": r["product"], "行数": r["n"], "起": r["a"], "止": r["b"]})
    for t in ["stock_financials", "shibor", "trading_calendar"]:
        dc = DATE_COL.get(t, "date")
        r = conn.execute(f"SELECT COUNT(*), MIN({dc}), MAX({dc}) FROM {t}").fetchone()
        rows.append({"表": t, "品种": "-", "行数": r[0], "起": r[1], "止": r[2]})
    return pd.DataFrame(rows)
