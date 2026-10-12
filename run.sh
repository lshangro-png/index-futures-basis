#!/bin/bash
# 从零到看到报告的完整链路。已建过库的话，重复跑是幂等的。
set -e
cd "$(dirname "$0")"

# 1) 从 data/ 下的 Excel/CSV 建库（期货、期权、ETF、利率）
python -m basis init

# 2) 聚宽补分红相关数据（需要 JQ_ACCOUNT / JQ_PASS）
if [ -n "$JQ_ACCOUNT" ] && [ -n "$JQ_PASS" ]; then
    python -m basis jq-backfill
else
    echo "跳过聚宽取数：未设置 JQ_ACCOUNT / JQ_PASS"
fi

# 3) 预计算分红预测（没有这步，图上不会有预测线）
python -m basis precompute

# 4) 出报告：results/ 下的 HTML + CSV。想交互看图就改成 jupyter notebook basis_analysis.ipynb
python -m basis report
