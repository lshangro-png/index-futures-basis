"""
report.py —— 可视化与导出：notebook 里的交互图、统计表，以及离线 HTML/CSV 报告
"""

import json
import os
from datetime import datetime

import pandas as pd

from .config import PRODUCTS, RESULTS_DIR, SLOT_LABEL, SLOTS

C_FUT, C_OPT, C_IOPT, C_DIV, C_PRED = "#2563eb", "#ea580c", "#0891b2", "#9333ea", "#7c3aed"
SLOT_C = {"00": "#2563eb", "01": "#f59e0b"}
HTML_SLOTS = ["00", "01"]          # HTML只出当月+次月，季月不画

# (列后缀, 中文名, 单位)
STAT_METRICS = [
    ("basis", "期货基差", "点"), ("basis_exdiv", "期货基差·除分红", "点"),
    ("basis_exdiv_pred", "期货基差·除预测分红", "点"),
    ("basis_opt", "ETF期权隐含", "点"), ("basis_opt_pred", "ETF期权隐含·减预测分红", "点"),
    ("basis_opt_exdiv", "ETF期权隐含·除分红", "点"),
    ("basis_iopt", "股指期权隐含", "点"), ("basis_iopt_exdiv", "股指期权隐含·除分红", "点"),
    ("basis_iopt_exdiv_pred", "股指期权隐含·除预测分红", "点"),
    ("ann", "期货年化", "%"), ("ann_exdiv", "期货年化·除分红", "%"), ("ann_exdiv_pred", "期货年化·除预测", "%"),
    ("ann_opt", "ETF期权年化", "%"), ("ann_opt_pred", "ETF期权年化·减预测分红", "%"),
    ("ann_opt_exdiv", "ETF期权年化·除分红", "%"),
    ("ann_iopt", "股指期权年化", "%"), ("ann_iopt_exdiv", "股指期权年化·除分红", "%"),
    ("ann_iopt_exdiv_pred", "股指期权年化·除预测", "%"),
    ("opt_minus_fut", "ETF期权−期货", "点"), ("opt_minus_fut_pred", "ETF期权−期货·预测分红", "点"),
    ("iopt_minus_fut", "股指期权−期货", "点"),
    ("div", "已实现分红", "点"), ("div_pred", "预测分红", "点"), ("div_err", "分红预测误差", "点"),
]


# ================================================================== 表

def summary_stats(res: pd.DataFrame, slots=SLOTS, metrics=STAT_METRICS) -> pd.DataFrame:
    """单品种各位次各指标的分布统计。当月年化受 DTE 放大影响，只看中位数"""
    rows = []
    for s in slots:
        for c, lab, unit in metrics:
            col = f"{s}_{c}"
            if col not in res.columns:
                continue
            v = pd.to_numeric(res[col], errors="coerce").dropna()
            if v.empty:
                continue
            rows.append({"位次": SLOT_LABEL[s], "指标": lab, "单位": unit, "样本": len(v),
                         "中位数": round(v.median(), 2), "均值": round(v.mean(), 2),
                         "标准差": round(v.std(), 2), "最小": round(v.min(), 2), "最大": round(v.max(), 2),
                         "p5": round(v.quantile(.05), 2), "p95": round(v.quantile(.95), 2)})
    return pd.DataFrame(rows)


def cross_product_table(results: dict, slot="00") -> pd.DataFrame:
    """跨品种对比（中位数口径）"""
    items = [("basis", "期货基差", "点"), ("basis_opt", "ETF期权隐含", "点"),
             ("basis_iopt", "股指期权隐含", "点"), ("opt_minus_fut", "ETF期权−期货", "点"),
             ("iopt_minus_fut", "股指期权−期货", "点"), ("div", "区间已实现分红", "点"),
             ("ann", "期货年化", "%"), ("ann_exdiv_pred", "期货年化·除预测分红", "%"),
             ("ann_opt", "ETF期权年化", "%"), ("ann_iopt", "股指期权年化", "%")]
    rows = []
    for c, lab, unit in items:
        row = {"指标": f"{SLOT_LABEL[slot]}{lab}", "单位": unit}
        for p, df in results.items():
            col = f"{slot}_{c}"
            v = pd.to_numeric(df[col], errors="coerce").dropna() if col in df.columns else pd.Series(dtype=float)
            row[PRODUCTS[p]["label"]] = round(float(v.median()), 2) if len(v) else None
        rows.append(row)
    return pd.DataFrame(rows)


# ================================================================== 面板定义（notebook 与 HTML 共用）

def panels_for_slot(s):
    """单个位次的面板。含分红=实线，除分红=虚线，两者间距就是分红"""
    return [
        ("基差：含分红 vs 除分红", "点", [
            (f"{s}_basis", "期货基差", C_FUT, "solid"),
            (f"{s}_basis_exdiv", "期货基差·除分红", C_FUT, "dash"),
            (f"{s}_basis_exdiv_pred", "期货·除【预测】分红", C_PRED, "dot"),
            (f"{s}_basis_opt", "ETF期权隐含", C_OPT, "solid"),
            (f"{s}_basis_opt_pred", "ETF期权隐含·减【预测】分红", C_OPT, "dot"),
            (f"{s}_basis_opt_exdiv", "ETF期权隐含·除分红", C_OPT, "dash"),
            (f"{s}_basis_iopt", "股指期权隐含", C_IOPT, "dashdot"),
            (f"{s}_basis_iopt_exdiv", "股指期权隐含·除分红", C_IOPT, "dash"),
            (f"{s}_basis_iopt_exdiv_pred", "股指期权·除【预测】分红", C_IOPT, "dot"),
        ]),
        ("年化基差：含分红 vs 除分红", "%", [
            (f"{s}_ann", "期货年化", C_FUT, "solid"),
            (f"{s}_ann_exdiv", "期货年化·除分红", C_FUT, "dash"),
            (f"{s}_ann_exdiv_pred", "期货年化·除【预测】", C_PRED, "dot"),
            (f"{s}_ann_opt", "ETF期权年化", C_OPT, "solid"),
            (f"{s}_ann_opt_pred", "ETF期权年化·减【预测】", C_OPT, "dot"),
            (f"{s}_ann_opt_exdiv", "ETF期权年化·除分红", C_OPT, "dash"),
            (f"{s}_ann_iopt", "股指期权年化", C_IOPT, "dashdot"),
            (f"{s}_ann_iopt_exdiv", "股指期权年化·除分红", C_IOPT, "dash"),
            (f"{s}_ann_iopt_exdiv_pred", "股指期权年化·除【预测】", C_IOPT, "dot"),
        ]),
        ("分红：事后已实现 vs 当日可预测", "点", [
            (f"{s}_div", "已实现（事后真值）", C_DIV, "solid"),
            (f"{s}_div_pred", "预测（当日可知）", C_PRED, "dash"),
            (f"{s}_div_err", "预测−已实现", "#94a3b8", "dot")]),
        ("期权隐含 − 期货（定价偏离）", "点", [
            (f"{s}_opt_minus_fut", "ETF期权−期货", C_OPT, "solid"),
            (f"{s}_opt_minus_fut_pred", "ETF期权−期货·预测分红", C_OPT, "dot"),
            (f"{s}_iopt_minus_fut", "股指期权−期货", C_IOPT, "dashdot")]),
        ("期权可用流动性", "", [
            (f"{s}_tradeable_vol", "可用成交量(张)", "#0891b2", "solid"),
            (f"{s}_tradeable_notional", "可用成交额(元)", "#be123c", "solid|y2")]),
    ]


def panels_term():
    """期限对比：当月 vs 次月。年化那张已消除天数差异，可直接横比"""
    return [
        ("期货基差：当月 vs 次月", "点",
         [(f"{s}_basis", SLOT_LABEL[s], SLOT_C[s], "solid") for s in HTML_SLOTS]),
        ("期权隐含基差：当月 vs 次月", "点",
         [(f"{s}_basis_opt", SLOT_LABEL[s] + "·ETF版", SLOT_C[s], "solid") for s in HTML_SLOTS]
         + [(f"{s}_basis_iopt", SLOT_LABEL[s] + "·股指版", SLOT_C[s], "dashdot") for s in HTML_SLOTS]),
        ("年化基差期限结构", "%",
         [(f"{s}_ann", SLOT_LABEL[s] + "期货", SLOT_C[s], "solid") for s in HTML_SLOTS]
         + [(f"{s}_ann_opt", SLOT_LABEL[s] + "ETF期权", SLOT_C[s], "dash") for s in HTML_SLOTS]),
    ]


def _build(df, defs):
    out = []
    for title, unit, traces in defs:
        ts = []
        for col, name, color, dash in traces:
            if col not in df.columns:
                continue
            v = pd.to_numeric(df[col], errors="coerce")
            if v.notna().sum() == 0:
                continue
            ts.append({"name": name, "color": color, "dash": dash.replace("|y2", ""),
                       "y2": dash.endswith("|y2"),
                       "y": [None if pd.isna(x) else round(float(x), 4) for x in v]})
        if ts:
            out.append({"title": title, "unit": unit, "traces": ts})
    return out


# ================================================================== notebook 交互图

def figure(res: pd.DataFrame, panel, height=380, title=None):
    """把一个面板定义画成 plotly 图，notebook 里 figure(...).show()"""
    import plotly.graph_objects as go
    ptitle, unit, _ = panel
    built = _build(res, [panel])
    fig = go.Figure()
    for t in (built[0]["traces"] if built else []):
        fig.add_trace(go.Scatter(x=res["date"], y=t["y"], name=t["name"], mode="lines",
                                 yaxis="y2" if t["y2"] else "y",
                                 line=dict(color=t["color"], width=1.6, dash=t["dash"])))
    fig.update_layout(height=height, title=title or f"{ptitle}" + (f"（{unit}）" if unit else ""),
                      hovermode="x unified", template="plotly_white",
                      margin=dict(l=60, r=60, t=50, b=40), legend=dict(orientation="h", y=-0.18))
    if any(t["y2"] for t in (built[0]["traces"] if built else [])):
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False))
    fig.update_yaxes(zeroline=True, zerolinecolor="#94a3b8")
    return fig


# ================================================================== HTML

_CSS = """
 body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      margin:0;padding:0 24px 40px;background:#f8fafc;color:#0f172a}
 .nav{position:sticky;top:0;background:#f8fafcf2;backdrop-filter:blur(6px);
      padding:14px 0 10px;border-bottom:1px solid #e2e8f0;margin-bottom:18px;z-index:9}
 .nav a{display:inline-block;padding:6px 15px;margin:2px 6px 2px 0;border:1px solid #cbd5e1;
        border-radius:6px;background:#fff;color:#0f172a;text-decoration:none;font-size:13px}
 .nav a.on{background:#2563eb;color:#fff;border-color:#2563eb}
 .nav a.jump{border-style:dashed;color:#0f766e}
 h1{font-size:20px;margin:16px 0 4px}
 h2{font-size:16px;margin:26px 0 10px;padding-left:9px;border-left:4px solid #2563eb}
 .sub{color:#64748b;font-size:13px;line-height:1.6}
 .card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:18px;overflow-x:auto}
 .hint{font-size:12px;color:#64748b;margin-bottom:10px}
 .pnl{margin-bottom:22px}
 .ptitle{font-size:13px;font-weight:600;color:#334155;margin:0 0 2px 4px}
 table{border-collapse:collapse;font-size:13px;width:100%}
 th,td{padding:5px 10px;border-bottom:1px solid #e2e8f0;text-align:right}
 th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
 th{color:#475569;font-weight:600;background:#f1f5f9;position:sticky;top:52px}
 td.neg{color:#dc2626} td.pos{color:#1d4ed8} td.med{font-weight:600;background:#f8fafc}
 tr.sep td{border-top:2px solid #cbd5e1}
"""

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>__TITLE__</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>__CSS__</style></head><body>
<div class="nav">__NAV__</div>
<h1>__TITLE__</h1>
<div class="sub">生成于 __TS__ · 拖拽框选放大时间区间，双击还原 · 点击图例可显示/隐藏单条线<br>
实线=含分红，虚线=除分红（两者间距即区间已实现分红）；点划线=股指期权版<br>
<b>点线</b>=用【预测分红】修正，只用当日可知信息，可用于回测；其余分红线是事后已实现值，含前视偏差，只能描述历史<br>
年化口径：基差/指数 × 365/剩余天数；期货用期货剩余天数，期权用期权自己的剩余天数</div>
__SECTIONS__
<h2 id="stats">摘要统计（全区间）</h2>
<div class="card"><div class="hint">当月年化受剩余天数放大，只看中位数（已加粗）</div><div id="stats"></div></div>
<script>
const DATA = __DATA__;
DATA.views.forEach((v,vi)=>{
  const host=document.getElementById('chart'+vi);
  host.innerHTML=v.panels.map((p,i)=>
    `<div class="pnl"><div class="ptitle">${p.title}${p.unit?' ('+p.unit+')':''}</div>`
    +`<div id="p${vi}_${i}"></div></div>`).join('');
  v.panels.forEach((p,i)=>{
    const tr=p.traces.map(t=>({
      x:DATA.dates, y:t.y, name:t.name, type:'scatter', mode:'lines',
      line:{color:t.color, width:t.dash==='dash'||t.dash==='dashdot'?1.4:1.8, dash:t.dash},
      connectgaps:false, yaxis:t.y2?'y2':'y',
      hovertemplate:'%{x}<br>%{y}'+(p.unit?' '+p.unit:'')+'<extra>'+t.name+'</extra>'}));
    const lay={height:280, margin:{l:64,r:p.traces.some(t=>t.y2)?64:20,t:8,b:40},
      hovermode:'x unified', showlegend:true,
      legend:{orientation:'h',y:-0.18,x:0,font:{size:11}},
      plot_bgcolor:'#fff', paper_bgcolor:'#fff', xaxis:{gridcolor:'#eef2f7'},
      yaxis:{gridcolor:'#eef2f7',zeroline:true,zerolinecolor:'#94a3b8',zerolinewidth:1,
             title:{text:p.unit||'',font:{size:11}}}};
    if(p.traces.some(t=>t.y2)){const s2=p.traces.find(t=>t.y2);
      lay.yaxis2={overlaying:'y',side:'right',showgrid:false,
        title:{text:s2.name,font:{size:10.5,color:s2.color}}, tickfont:{size:9.5,color:s2.color}};}
    Plotly.newPlot('p'+vi+'_'+i, tr, lay, {responsive:true, displaylogo:false,
      modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d']});
  });
});
const c=v=>`<td class="${v<0?'neg':(v>0?'pos':'')}">${v}</td>`; let prev=null;
document.getElementById('stats').innerHTML=
  '<table><tr><th>合约</th><th>指标</th><th>单位</th><th>样本</th><th>中位数</th>'
  +'<th>均值</th><th>标准差</th><th>最小</th><th>最大</th></tr>'
  + DATA.stats.map(r=>{const sp=(prev&&prev!==r.term)?' class="sep"':''; prev=r.term;
      return `<tr${sp}><td>${r.term}</td><td>${r.label}</td><td>${r.unit}</td><td>${r.n}</td>`
        + `<td class="med ${r.median<0?'neg':(r.median>0?'pos':'')}">${r.median}</td>`
        + c(r.mean) + `<td>${r.std}</td>` + c(r.min) + c(r.max) + '</tr>';}).join('')
  + '</table>';
</script></body></html>"""

_INDEX = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>基差分析报告</title>
<style>__CSS__</style></head><body>
<div class="nav">__NAV__</div><h1>股指基差分析报告</h1>
<div class="sub">生成于 __TS__ · 点击上方品种进入分品种报告</div>
<h2>跨品种对比（当月，中位数口径）</h2>
<div class="card">__TABLE__</div></body></html>"""


def _nav(products, cur=None, views=None):
    on = lambda flag: ' class="on"' if flag else ""
    a = [f'<a href="basis_report.html"{on(cur is None)}>总览</a>']
    a += [f'<a href="basis_report_{p}.html"{on(p == cur)}>{PRODUCTS[p]["label"]}</a>' for p in products]
    if views:
        a += [f'<a class="jump" href="#v{i}">{v["label"]}</a>' for i, v in enumerate(views)]
        a += ['<a class="jump" href="#stats">摘要</a>']
    return "".join(a)


def _html_table(df: pd.DataFrame):
    def cell(c):
        if isinstance(c, (int, float)) and not pd.isna(c):
            cls = "neg" if c < 0 else ("pos" if c > 0 else "")
            return f'<td class="{cls}">{c:.2f}</td>'
        return f"<td>{'—' if c is None or (isinstance(c, float) and pd.isna(c)) else c}</td>"
    th = "".join(f"<th>{h}</th>" for h in df.columns)
    body = "".join("<tr>" + "".join(cell(c) for c in r) + "</tr>" for r in df.itertuples(index=False))
    return f"<table><tr>{th}</tr>{body}</table>"


def render_html(results: dict, out_dir=RESULTS_DIR):
    """总览页 + 每品种一页，离线可开（图表库走 CDN）"""
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    valid = {p: d for p, d in results.items() if d is not None and not d.empty}
    if not valid:
        print("✗ 无可导出数据")
        return

    for product, df in valid.items():
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date")
        views = [{"label": SLOT_LABEL[s], "panels": pans}
                 for s in HTML_SLOTS if (pans := _build(d, panels_for_slot(s)))]
        term = _build(d, panels_term())
        if term:
            views.append({"label": "期限对比", "panels": term})
        if not views:
            continue
        stats = summary_stats(d, HTML_SLOTS)
        stats = [{"term": r["位次"], "label": r["指标"], "unit": r["单位"], "n": int(r["样本"]),
                  "median": r["中位数"], "mean": r["均值"], "std": r["标准差"],
                  "min": r["最小"], "max": r["最大"]} for _, r in stats.iterrows()]
        secs = "".join(f'<h2 id="v{i}">{v["label"]}</h2><div class="card"><div id="chart{i}"></div></div>'
                       for i, v in enumerate(views))
        data = {"dates": d["date"].dt.strftime("%Y-%m-%d").tolist(), "views": views, "stats": stats}
        label = PRODUCTS[product]["label"]
        html = (_PAGE.replace("__CSS__", _CSS).replace("__NAV__", _nav(valid, product, views))
                .replace("__TITLE__", f"{label} 基差分析").replace("__SECTIONS__", secs)
                .replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace("__TS__", ts))
        fp = os.path.join(out_dir, f"basis_report_{product}.html")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  📊 {label}: {len(d)}行, {len(views)}组视图 → {os.path.basename(fp)} "
              f"({os.path.getsize(fp)/1024:.0f} KB)")

    idx = (_INDEX.replace("__CSS__", _CSS).replace("__NAV__", _nav(valid))
           .replace("__TABLE__", _html_table(cross_product_table(valid))).replace("__TS__", ts))
    ip = os.path.join(out_dir, "basis_report.html")
    with open(ip, "w", encoding="utf-8") as f:
        f.write(idx)
    print(f"  ✓ 总览页: {ip}")


def export_csv(results: dict, out_dir=RESULTS_DIR):
    os.makedirs(out_dir, exist_ok=True)
    for p, r in results.items():
        r.to_csv(os.path.join(out_dir, f"indicators_{p}.csv"), index=False, encoding="utf-8")
    print("  ✓ 已导出:", ", ".join(f"indicators_{p}.csv" for p in results))
