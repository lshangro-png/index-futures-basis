"""
importers.py —— 从手工导出的 CSV / Excel 建库

    python -m basis init --data ./data/

data/ 下的文件约定见 README。
"""

import os
import re

import pandas as pd

from . import db
from .config import PRODUCTS, SLOTS

_d = db.to_date_str


def import_combine(conn, path: str, product: str, verbose=True) -> dict:
    """导入 {IF,IH,IC,IM}_combine.csv（双层表头）

    提供：指数收盘、股息点指数、四个合约位次的行情/最后交易日/区间分红点数。
    """
    raw = pd.read_csv(path, header=[0, 1])
    raw.columns = [f"{str(a).strip()}|{str(b).strip()}" for a, b in raw.columns]
    raw = raw.rename(columns={raw.columns[0]: "date"})
    raw["date"] = _d(raw["date"])
    raw = raw.dropna(subset=["date"])
    cfg, stat = PRODUCTS[product], {}
    num = lambda f, t: pd.to_numeric(raw[f"{f}|{t}"], errors="coerce") if f"{f}|{t}" in raw.columns else None

    s = num("s_dq_close", cfg["index"])
    if s is not None:
        stat["index"] = db.upsert(conn, "index_daily", pd.DataFrame(
            {"product": product, "date": raw["date"], "close": s}).dropna(subset=["close"]))

    if cfg["divpt"]:
        v = num("s_dq_close", cfg["divpt"])
        if v is not None:
            stat["divpt"] = db.upsert(conn, "dividend_point_index", pd.DataFrame(
                {"product": product, "date": raw["date"], "value": v}).dropna(subset=["value"]))

    db.upsert(conn, "trading_calendar", pd.DataFrame({"date": raw["date"].unique()}))

    frames = []
    for i in SLOTS:
        tag = f"{cfg['fut']}{i}"
        if f"CLOSEPRICE|{tag}" not in raw.columns:
            continue
        frames.append(pd.DataFrame({
            "product": product, "date": raw["date"], "contract": tag,
            "contract_code": raw.get(f"CONTRACTCODE|{tag}"),
            "close": num("CLOSEPRICE", tag), "settle": num("SETTLEPRICE", tag),
            "open_interest": num("OPENINTEREST", tag), "volume": num("TURNOVERVOLUME", tag),
            "turnover": num("TURNOVERVALUE", tag),
            "last_trading_date": _d(raw.get(f"LASTTRADINGDATE|{tag}")),
            "days": num("days", tag), "div_points": num("Div", tag),
        }).dropna(subset=["close"]))
    if frames:
        stat["futures"] = db.upsert(conn, "futures_daily", pd.concat(frames, ignore_index=True))

    if verbose:
        print(f"  [{product}] {os.path.basename(path)}: " + ", ".join(f"{k}={v}" for k, v in stat.items()))
    return stat


def import_excel(conn, path: str, product: str, verbose=True) -> dict:
    """导入 {product}.xlsx 中 combine 没有的部分：ETF行情 / SHIBOR / ETF期权行情"""
    xls = pd.ExcelFile(path)
    sheet = lambda n: pd.read_excel(xls, sheet_name=n) if n in xls.sheet_names else None
    stat = {}

    df = sheet(f"{product}ETF行情")
    if df is not None and {"date", "etf_close"} <= set(df.columns):
        stat["etf"] = db.upsert(conn, "etf_daily", pd.DataFrame({
            "product": product, "date": _d(df["date"]),
            "close": pd.to_numeric(df["etf_close"], errors="coerce")}).dropna())

    df = sheet(f"{product}SHIBOR")
    if df is not None and "date" in df.columns:
        rs = [pd.DataFrame({"date": _d(df["date"]), "tenor": str(c),
                            "value": pd.to_numeric(df[c], errors="coerce")})
              for c in df.columns if str(c).lower().startswith("shibor")]
        if rs:
            stat["shibor"] = db.upsert(conn, "shibor", pd.concat(rs, ignore_index=True).dropna())

    df = sheet(f"{product}交易日历")
    if df is not None and "date" in df.columns:
        db.upsert(conn, "trading_calendar", pd.DataFrame({"date": _d(df["date"])}).dropna())

    df = sheet("期权数据")
    if df is None:
        df = sheet(f"{product}期权行情")
    if df is not None:
        df = df.rename(columns={"日期": "date", "合约代码": "option_code", "收盘价": "close",
                                "成交量": "volume", "成交额": "notional", "合约名称": "contract_name"})
        if {"date", "option_code", "close"} <= set(df.columns):
            q = pd.DataFrame({
                "product": product, "date": _d(df["date"]),
                "option_code": df["option_code"].astype(str).str.strip(),
                "contract_name": df.get("contract_name"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
                "volume": pd.to_numeric(df.get("volume"), errors="coerce"),
                "notional": pd.to_numeric(df.get("notional"), errors="coerce"),
            }).dropna(subset=["date", "option_code", "close"])
            q = q.drop_duplicates(subset=["product", "date", "option_code"], keep="last")
            stat["option"] = db.upsert(conn, "option_quotes", q)

    if verbose:
        print(f"  [{product}] {os.path.basename(path)}: " +
              (", ".join(f"{k}={v}" for k, v in stat.items()) or "无可导入内容"))
    return stat


# 股指期权代码格式：{前缀}{YYMM}-{C|P}-{行权价}.CFE，比如 IO2608-C-4050.CFE
IDX_OPT_CODE_RE = re.compile(r"^[A-Z]{1,2}(\d{4})-?([CP])-?(\d+(?:\.\d+)?)(?:\.CFE)?$")


def parse_index_option_codes(codes: pd.Series) -> pd.DataFrame:
    """合约代码 → contract_month / opt_type / strike 三列（解析失败为空）"""
    p = codes.astype(str).str.strip().str.extract(IDX_OPT_CODE_RE)
    return pd.DataFrame({"contract_month": p[0], "opt_type": p[1],
                         "strike": pd.to_numeric(p[2], errors="coerce")}, index=codes.index)


def import_index_options(conn, path: str, product: str, sheet_name=None, verbose=True) -> dict:
    """导入股指期权(IO/HO/MO)逐合约行情，长表格式：
    合约代码/合约名称/日期/收盘价/成交量/成交额/(状态)

    sheet_name 不传时依次尝试 "期权数据_股指"（挂在 {product}.xlsx 里）
    和 "期权数据"（独立文件 {product}期权.xlsx）。
    """
    xls = pd.ExcelFile(path)
    candidates = [sheet_name] if sheet_name else ["期权数据_股指", "期权数据"]
    name = next((n for n in candidates if n in xls.sheet_names), None)
    if name is None:
        if verbose:
            print(f"  [{product}] {os.path.basename(path)}: 没找到股指期权sheet（试过 {candidates}），跳过")
        return {}
    df = pd.read_excel(xls, sheet_name=name)

    df = df.rename(columns={"日期": "date", "合约代码": "option_code", "收盘价": "close",
                            "成交量": "volume", "成交额": "turnover", "合约名称": "contract_name"})
    need = {"date", "option_code", "close"}
    if not need <= set(df.columns):
        if verbose:
            print(f"  [{product}] {os.path.basename(path)}: 股指期权sheet缺列 {need - set(df.columns)}")
        return {}

    codes = df["option_code"].astype(str).str.strip()
    parsed = parse_index_option_codes(codes)
    bad = parsed["contract_month"].isna().sum()
    if bad and verbose:
        sample = codes[parsed["contract_month"].isna()].head(3).tolist()
        print(f"  [{product}] 警告：{bad} 行合约代码解析失败（示例 {sample}），"
              f"这些行 contract_month/opt_type/strike 会是空值")

    q = pd.concat([pd.DataFrame({"product": product, "date": _d(df["date"]), "option_code": codes}),
                   parsed,
                   pd.DataFrame({"close": pd.to_numeric(df["close"], errors="coerce"),
                                 "volume": pd.to_numeric(df.get("volume"), errors="coerce"),
                                 "turnover": pd.to_numeric(df.get("turnover"), errors="coerce")})],
                  axis=1).dropna(subset=["date", "option_code", "close"])
    q = q.drop_duplicates(subset=["product", "date", "option_code"], keep="last")
    n = db.upsert(conn, "index_option_quotes", q)
    if verbose:
        print(f"  [{product}] {os.path.basename(path)}: index_option={n}")
    return {"index_option": n}


def init_all(conn, data_dir: str):
    """把 data/ 下能认出来的文件全部导入"""
    print(f"导入 combine CSV（指数/期货/股息点/到期日/分红点数）")
    for p, cfg in PRODUCTS.items():
        for c in [f"{cfg['fut']}_combine.csv", f"{p}_combine.csv"]:
            fp = os.path.join(data_dir, c)
            if os.path.exists(fp):
                import_combine(conn, fp, p)
                break
        else:
            print(f"  [{p}] 未找到 {cfg['fut']}_combine.csv")

    print("\n导入 Excel（ETF行情/SHIBOR/期权行情）")
    for p in PRODUCTS:
        fp = os.path.join(data_dir, f"{p}.xlsx")
        if os.path.exists(fp):
            import_excel(conn, fp, p)
            # 股指期权可能挂在 {p}.xlsx 的"期权数据_股指"sheet（50的做法）；大多数品种没有，不提示
            import_index_options(conn, fp, p, sheet_name="期权数据_股指", verbose=False)
        else:
            print(f"  [{p}] 未找到 {p}.xlsx")
        # 也可能是独立文件 {p}期权.xlsx（300/1000的做法）
        fp_opt = os.path.join(data_dir, f"{p}期权.xlsx")
        if os.path.exists(fp_opt):
            import_index_options(conn, fp_opt, p, sheet_name="期权数据")
