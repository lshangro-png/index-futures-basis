"""
config.py —— 全项目共享的常量。改参数只改这里。
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "basis.db")
DATA_DIR = os.path.join(ROOT, "data")
RESULTS_DIR = os.path.join(ROOT, "results")

# 品种 -> 指数代码 / 期货前缀 / 中证股息点指数 / ETF
# idx_opt：股指期权代码前缀。50/300/1000 有（HO/IO/MO），500 目前没有股指期权，只能用ETF期权。
PRODUCTS = {
    "50":   {"index": "000016.SH", "fut": "IH", "divpt": "h50060.CSI", "etf": "510050.SH", "idx_opt": "HO", "label": "上证50"},
    "300":  {"index": "000300.SH", "fut": "IF", "divpt": "h30401.CSI", "etf": "510300.SH", "idx_opt": "IO", "label": "沪深300"},
    "500":  {"index": "000905.SH", "fut": "IC", "divpt": "h30402.CSI", "etf": "510500.SH", "idx_opt": None, "label": "中证500"},
    "1000": {"index": "000852.SH", "fut": "IM", "divpt": None,         "etf": None,        "idx_opt": "MO", "label": "中证1000"},
}

SLOTS = ["00", "01", "02", "03"]
SLOT_LABEL = {"00": "当月", "01": "次月", "02": "季月1", "03": "季月2"}

# ---------------------------------------------------------------- 基差计算
DAYS_PER_YEAR = 365
CONTRACT_MULTIPLIER = 10000      # ETF期权合约乘数，用于识别成交额单位
TRADEABLE_RATIO = 0.05           # 可用流动性 = min(购,沽)成交 × 该比例
N_STRIKES_SIDE = 5               # PCP 反推取平值上下各几档

# ETF 自身除息识别（指数/ETF 比值阶跃）
ETF_JUMP_MIN, ETF_JUMP_MAX = 0.4, 5.0   # 单日跳升幅度区间(%)
ETF_KEEP = 0.7                          # 跳升后需保持的比例
ETF_SNR = 2.0                           # 跳升/近20日噪声
ETF_GAP = 15                            # 两次除息最少间隔(交易日)
ETF_DIST_NOTICE_DAYS = 5                # ETF 分红公告提前除息日的交易日数：此前视为未知，不加回

# ---------------------------------------------------------------- 分红预测
# 德邦报告 2.3：阈值取全A所有间隔天数标准差的均值
MAD_PLAN = 20           # 预案公告日 → 除息日
MAD_SGM = 11            # 股东大会公告日 → 除息日
DPS_LINEAR_TOL = 0.1    # 报告 3.2：每股股利线性判定阈值
# 报告期纳入预测的门槛：近 LOOKBACK_YEARS 年内至少出现 MIN_OCCURRENCE 次。
# 报告未讨论此问题（它只做年报分红），但实测不加门槛会因偶发特别股息而高估。
LOOKBACK_YEARS = 3
MIN_OCCURRENCE = 2
# 在途预案（dividend_pending）：预案发出后多少天内视为仍在推进；超过则不再采信
PENDING_MAX_DAYS = 365
# 进度含这些字样的在途预案不采信（已停止/被否决）。
# 主判据是聚宽的取消公告日 bonus_cancel_pub_date（按日期 point-in-time 生效），关键字只是兜底
PENDING_DEAD_WORDS = ("停止", "终止", "取消", "否决", "未通过", "不实施")
# 进度含这些字样、或每股分红明确为 0 的，视为「本期不分红」，该报告期不再做预测
PENDING_NO_DIV_WORDS = ("不分配", "不分红", "不派")
# 报告 2.3(5)：97% 的公司在除息前 10 天内发实施公告。当日还没发实施公告的，
# 预测除息日至少在当日之后这么多天（已出预案的顺延到这里，连预案都没有的改用默认日期）
MIN_DAYS_TO_EX = 10
NP_RATIO_TOL = 0.1      # 报告 3.3.1：前三季度利润占全年比例的最大偏离度阈值


def parse_products(items):
    """--products 空格分隔("300 50")或逗号分隔("300,50")都认；空则全部"""
    if not items:
        return list(PRODUCTS)
    if isinstance(items, str):
        items = [items]
    prods = [x.strip() for item in items for x in str(item).split(",") if x.strip()]
    bad = [p for p in prods if p not in PRODUCTS]
    if bad:
        raise SystemExit(f"未知品种 {bad}，可选: {list(PRODUCTS)}")
    return prods
