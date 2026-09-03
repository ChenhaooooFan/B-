# -*- coding: utf-8 -*-
"""
NailVesta 飞机袋消耗测算（4 来源：TikTok + 独立站 + 深度达人单 + 普通水单）
先手动配置 3 个规则 → 上传订单表 → 自动算「过去 7 天 / 过去 14 天」的大、小飞机袋日均。

手动规则：
  ① 满多少钱送什么（满额赠）—— 满 X 元(实付) 送 折叠盒/卸甲笔/美甲册/指甲
  ② 买几副送什么（买副赠）  —— 买满 X 副 送 折叠盒/卸甲笔/美甲册/指甲
  ③ 买 3 副用大袋还是小袋   —— 仓库有时会变，手动切换

打包规则（袋型）：
  基础：1~2 副→小飞机袋；3~6 副→大飞机袋；>6 副→每 6 副 1 个大袋，余数按下方判断。
       （规则③选“小袋”时，小袋可装到 3 副，余数 3 也进小袋）
  配件占位（大袋总容量=6 位）：折叠盒 6 位(占满一个大袋)、美甲册 4 位、卸甲笔 2 位。

数据口径：
  · TikTok：1 Order ID = 1 单；指甲 SKU 行数 = 副数。免费赠指甲($0 行)已在表内，自动计入。
  · 独立站：1 订单号 = 1 单；指甲 Lineitem 行数 = 副数。免费赠指甲不在表内 → 用“买副赠·指甲”补。
  · 深度达人单 / 普通水单：1 handle(或 Order ID) = 1 单；款式名数(逗号分隔) = 副数。
  · 只排除取消单，零元单全部保留。
  · 水单是发货清单(已含实发赠品) → 不再叠加满额/买副赠品，只按副数装袋。
"""

import re
from datetime import date, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="NailVesta 飞机袋消耗测算", page_icon="✈️", layout="wide")

# ============ 物料 / 打包配置 ============
BIG_CAP = 6  # 大飞机袋 = 6 个指甲位

# 配件 Seller SKU → (显示名, 占用指甲位)
ACCESSORY_POS = {
    "NSB001": ("折叠盒 Storage Box", 6),
    "NOB001": ("美甲册 Binder", 4),
    "NOB002": ("美甲册 Binder", 4),
    "NRP001": ("卸甲笔 Remover Pen", 2),
    "NPK001": ("灯胶套装/ProTouch Kit", 2),
    "NVT001": ("工具套装 Toolkit", 2),
}
BOX_SKUS = {"NSB001"}                       # 占满一整个大飞机袋
CHANNELS_WITH_PRICE = {"TikTok", "独立站"}   # 有金额、可加赠品的渠道（水单不加）

# 赠品 → (处理方式, 每份占位/副数)
GIFT_KINDS = {
    "折叠盒": ("box", 6),
    "卸甲笔": ("pen", 2),
    "美甲册": ("binder", 4),
    "指甲":   ("nail", 1),
}
CHANNEL_OPTS = ["全部(TK+独立站)", "TikTok", "独立站"]


def to_float(x):
    try:
        return float(str(x).strip().replace(",", ""))
    except Exception:
        return 0.0


# 不同 pandas 版本把缺失值字符串化成 nan / NaN / <NA> 各不相同，统一按此判空
NA_STRS = {"", "nan", "nat", "none", "<na>", "null"}


def _blank(series):
    """返回布尔 Series：True=该格为空/缺失。
    先转可空字符串，用 isna() 抓真正的缺失(pandas 3.x 里 NA 不会被 astype(str) 变成 'nan')，
    再兜底匹配字面量 nan/NaN/<NA>/空串等，兼容 pandas 1.x~3.x。"""
    s = series.astype("string")
    return s.isna() | s.str.strip().str.lower().isin(NA_STRS)


def styles_of(cell):
    """把 '款式A, 款式B' 拆成 ['款式A','款式B']。"""
    return [s.strip() for s in str(cell or "").split(",") if s.strip()]


# 水单按「款式名」识别配件（用词边界，避免 Aspen 之类色名误伤 pen/box）
# 返回 (类型, 占位)：nail=指甲(1副)；box=折叠盒(占满大袋)；其余并入占位池
WATERSLIP_ACC = [
    (r"\bbinder\b|\borganizer\b", "binder", 4),
    (r"\bstorage box\b|\bbox\b", "box", 6),
    (r"\btoolkit\b|\bkit\b", "kit", 2),
    (r"\bremover\b", "remover", 2),
    (r"\bpen\b", "pen", 2),
]


def classify_style(name):
    s = str(name).lower()
    for pat, kind, pos in WATERSLIP_ACC:
        if re.search(pat, s):
            return kind, pos
    return "nail", 1


def water_counts(cell):
    """一行款式单元格 → (指甲副数, 折叠盒数, 其它配件占位)。"""
    n_nail = n_box = extra = 0
    for s in styles_of(cell):
        kind, pos = classify_style(s)
        if kind == "nail":
            n_nail += 1
        elif kind == "box":
            n_box += 1
        else:
            extra += pos
    return n_nail, n_box, extra


def acc_pos_of(sku):
    return ACCESSORY_POS.get(str(sku).strip(), (None, 0))[1]


def is_box(sku):
    return str(sku).strip() in BOX_SKUS


def is_nail(sku):
    return str(sku).strip() not in ACCESSORY_POS


def pack_order(n_nails, n_box, extra_pos, small_max):
    """返回 (大飞机袋数, 小飞机袋数)。small_max = 小袋最多装几副(2 或 3)。"""
    big = int(n_box)
    small = 0
    pool = int(round(n_nails)) + int(round(extra_pos))
    big += pool // BIG_CAP
    rem = pool % BIG_CAP
    if rem == 0:
        pass
    elif rem <= small_max:
        small += 1
    else:
        big += 1
    return big, small


# ============ 4 个解析器 → 统一「订单级」表 ============
# 统一列：order_id, date, canceled, n_nails, n_box, extra_pos, subtotal, order_amount, channel

def _build_waterslip(df, key_col, style_col, channel):
    """水单通用解析：key_col=订单标识，style_col=款式(逗号分隔)。
    款式里若出现 storage box/binder/kit/remover/pen 等配件名，按占位规则算(不当指甲副)。"""
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    df["_key"] = df[key_col].astype(str).str.strip()
    df = df[df["_key"] != ""]
    comp = df[style_col].map(water_counts)
    df["_nail"] = comp.map(lambda t: t[0])
    df["_box"] = comp.map(lambda t: t[1])
    df["_extra"] = comp.map(lambda t: t[2])
    df["_date"] = pd.to_datetime(df["日期"].astype(str).str.strip(),
                                 errors="coerce", format="mixed").dt.date
    g = df.groupby("_key", sort=False)
    out = pd.DataFrame({
        "order_id": g.size().index,
        "date": g["_date"].first().values,
        "n_nails": g["_nail"].sum().values,
        "n_box": g["_box"].sum().values,
        "extra_pos": g["_extra"].sum().values,
    })
    out["canceled"] = False
    out["subtotal"] = 0.0
    out["order_amount"] = 0.0
    out["channel"] = channel
    return out


def normalize_tiktok(df):
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    df["sku"] = df["Seller SKU"].astype(str).str.strip()
    df["_nail"] = df["sku"].map(is_nail)
    df["_box"] = df["sku"].map(is_box)
    df["_extra"] = df.apply(lambda r: 0 if r["_box"] else acc_pos_of(r["sku"]), axis=1)
    df["_sub"] = df["SKU Subtotal After Discount"].map(to_float)
    df["_date"] = pd.to_datetime(df["Created Time"].astype(str).str.strip(),
                                 errors="coerce", format="mixed").dt.date
    g = df.groupby("Order ID", sort=False)
    out = pd.DataFrame({
        "order_id": g.size().index,
        "date": g["_date"].first().values,
        "canceled": (g["Order Status"].first().astype(str).str.strip() == "Canceled").values,
        "n_nails": g["_nail"].sum().values,     # 指甲 SKU 行数
        "n_box": g["_box"].sum().values,
        "extra_pos": g["_extra"].sum().values,
        "subtotal": g["_sub"].sum().values,     # 商品小计(不含运费税)
        "order_amount": g["Order Amount"].first().map(to_float).values,  # 实付总额
    })
    out["channel"] = "TikTok"
    return out


def normalize_shopify(df):
    """标准 Shopify 导出：订单级字段(Subtotal/Total/Created at/Cancelled at)只在每单首行。"""
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]

    def col(*cands):
        for c in cands:
            if c in df.columns:
                return c
        return None

    c_id = col("Name", "Order Name", "订单号", "Order ID")
    c_sku = col("Lineitem sku", "Lineitem SKU", "SKU", "Seller SKU")
    c_date = col("Created at", "Paid at", "Created Time")
    c_cancel = col("Cancelled at", "Canceled at")
    c_fin = col("Financial Status", "financial_status")
    c_sub = col("Subtotal", "Subtotal Price")
    c_total = col("Total", "Total Price")

    df["sku"] = df[c_sku].astype(str).str.strip() if c_sku else ""
    df["_nail"] = df["sku"].map(is_nail)          # 每个 Lineitem 行 = 1 副
    df["_box"] = df["sku"].map(is_box)
    df["_extra"] = df.apply(lambda r: 0 if r["_box"] else acc_pos_of(r["sku"]), axis=1)
    if c_date:
        # Shopify 时间戳带时区偏移(如 -0700/-0800，夏令时切换会混用两种)。
        # pandas 2.x 遇到混合偏移会报 "Mixed timezones detected"；这里去掉偏移按本地时间解析，
        # 保住下单当天的本地日期(用 utc=True 会把傍晚单推到次日 UTC，日期就错了)。
        _d = df[c_date].astype(str).str.strip().str.replace(
            r"\s*[+-]\d{2}:?\d{2}$", "", regex=True)
        df["_date"] = pd.to_datetime(_d, errors="coerce", format="mixed")
    else:
        df["_date"] = pd.NaT
    df["_sub"] = df[c_sub].map(to_float) if c_sub else 0.0
    df["_total"] = df[c_total].map(to_float) if c_total else 0.0
    df["_cancel"] = (~_blank(df[c_cancel])) if c_cancel else False  # 有取消时间=真取消
    df["_fin"] = df[c_fin].astype(str).str.strip().str.lower() if c_fin else ""

    g = df.groupby(c_id, sort=False)
    out = pd.DataFrame({
        "order_id": g.size().index,
        "date": g["_date"].max().dt.date.values,
        "n_nails": g["_nail"].sum().values,
        "n_box": g["_box"].sum().values,
        "extra_pos": g["_extra"].sum().values,
        "subtotal": g["_sub"].max().values,       # 商品小计(不含运费税)
        "order_amount": g["_total"].max().values,  # 实付总额
    })
    cancel = g["_cancel"].apply(lambda s: bool(s.any())).values
    void = g["_fin"].apply(lambda s: any(x in ("voided", "cancelled", "canceled") for x in s)).values
    out["canceled"] = cancel | void
    out["channel"] = "独立站"
    return out


def normalize_daren(df):
    """深度达人单：Handle=订单，款式(逗号分隔)=款式；配件名按占位算。"""
    return _build_waterslip(df, "Handle", "款式", "达人单")


def normalize_putong(df):
    """普通水单【客人+广达】：Order ID=订单，Product Name(逗号分隔)=款式；配件名按占位算。"""
    return _build_waterslip(df, "Order ID", "Product Name", "普通水单")


def read_any(file):
    if file.name.lower().endswith(".xlsx"):
        return pd.read_excel(file, dtype=str)
    return pd.read_csv(file, dtype=str, encoding="utf-8-sig")


def apply_rules_and_pack(od, spend_rules, buy_rules, metric_col, small_max):
    """对合并后的订单级表应用赠品规则(仅 TK/独立站)并装袋，返回带 big/small 的表。"""
    od = od.copy()
    od["n_nails_purchased"] = od["n_nails"]        # 买副赠门槛用「加赠前」副数
    for c in ["g_box", "g_pen", "g_binder", "g_nail"]:
        od[c] = 0

    def channel_mask(rule_ch):
        if rule_ch == "全部(TK+独立站)":
            return od["channel"].isin(list(CHANNELS_WITH_PRICE))
        return od["channel"] == rule_ch

    def grant(mask, gift, qty):
        kind, pos = GIFT_KINDS[gift]
        qty = int(qty)
        if kind == "box":
            od.loc[mask, "n_box"] += qty; od.loc[mask, "g_box"] += qty
        elif kind == "pen":
            od.loc[mask, "extra_pos"] += pos * qty; od.loc[mask, "g_pen"] += qty
        elif kind == "binder":
            od.loc[mask, "extra_pos"] += pos * qty; od.loc[mask, "g_binder"] += qty
        elif kind == "nail":
            od.loc[mask, "n_nails"] += qty; od.loc[mask, "g_nail"] += qty

    def valid_rows(rules):
        if rules is None:
            return
        for _, r in rules.iterrows():
            if not bool(r.get("启用")) or r.get("赠品") not in GIFT_KINDS:
                continue
            if pd.isna(r.get("开始日期")) or pd.isna(r.get("结束日期")):
                continue
            yield r

    for r in valid_rows(spend_rules):
        m = channel_mask(r["适用渠道"]) & od["date"].between(r["开始日期"], r["结束日期"]) \
            & (od[metric_col] >= to_float(r["满额门槛"]))
        grant(m, r["赠品"], r.get("赠送数量", 1) or 1)

    for r in valid_rows(buy_rules):
        m = channel_mask(r["适用渠道"]) & od["date"].between(r["开始日期"], r["结束日期"]) \
            & (od["n_nails_purchased"] >= int(r["买满几副"]))
        grant(m, r["赠品"], r.get("赠送数量", 1) or 1)

    packed = od.apply(
        lambda r: pack_order(r["n_nails"], r["n_box"], r["extra_pos"], small_max),
        axis=1, result_type="expand")
    od["big"], od["small"] = packed[0], packed[1]
    return od


# ============ 侧边栏：口径 ============
st.sidebar.header("⚙️ 计算口径")
metric_choice = st.sidebar.radio(
    "满额门槛看哪个金额（实付）",
    ["商品小计（不含运费税）", "订单实付总额（含运费税）"], index=0)
st.sidebar.caption("只排除取消单；零元单全部保留。水单不叠加赠品(发货清单已含实发)。")

# ============ 主体 ============
st.title("✈️ NailVesta 飞机袋消耗测算")
st.caption("① 先配置 3 个规则 → ② 上传订单表(最多 4 份) → 自动算过去 7 天 / 14 天日均。")

# ---------- 第一步：规则 ----------
st.markdown("## ① 规则设置")

st.markdown("**规则③：买 3 副用哪种袋子**（仓库有时会变）")
bag3_choice = st.radio(
    "买 3 副装：", ["大飞机袋（默认）", "小飞机袋"], index=0, horizontal=True,
    label_visibility="collapsed")
SMALL_MAX = 3 if bag3_choice.startswith("小") else 2  # 小袋最多装几副
st.caption(f"当前：3 副 → {'小飞机袋（小袋最多装 3 副）' if SMALL_MAX==3 else '大飞机袋（小袋最多装 2 副）'}")

rc1, rc2 = st.columns(2)
with rc1:
    st.markdown("**规则①：满多少钱送什么**（满额赠）")
    spend_rules = st.data_editor(
        pd.DataFrame([
            {"启用": True, "适用渠道": "全部(TK+独立站)", "赠品": "折叠盒", "赠送数量": 1,
             "满额门槛": 99.99, "开始日期": date(2026, 8, 14), "结束日期": date(2026, 8, 14)},
            {"启用": True, "适用渠道": "全部(TK+独立站)", "赠品": "卸甲笔", "赠送数量": 1,
             "满额门槛": 89.00, "开始日期": date(2026, 8, 25), "结束日期": date(2026, 8, 27)},
        ]),
        num_rows="dynamic", use_container_width=True, key="spend_rules",
        column_config={
            "启用": st.column_config.CheckboxColumn(width="small"),
            "适用渠道": st.column_config.SelectboxColumn(options=CHANNEL_OPTS),
            "赠品": st.column_config.SelectboxColumn(options=list(GIFT_KINDS.keys())),
            "赠送数量": st.column_config.NumberColumn(min_value=1, step=1, width="small"),
            "满额门槛": st.column_config.NumberColumn(format="%.2f"),
            "开始日期": st.column_config.DateColumn(format="MM-DD"),
            "结束日期": st.column_config.DateColumn(format="MM-DD"),
        })
with rc2:
    st.markdown("**规则②：买几副送什么**（买副赠）")
    buy_rules = st.data_editor(
        pd.DataFrame([
            {"启用": False, "适用渠道": "独立站", "赠品": "指甲", "赠送数量": 1,
             "买满几副": 3, "开始日期": date(2026, 8, 14), "结束日期": date(2026, 8, 27)},
        ]),
        num_rows="dynamic", use_container_width=True, key="buy_rules",
        column_config={
            "启用": st.column_config.CheckboxColumn(width="small"),
            "适用渠道": st.column_config.SelectboxColumn(options=CHANNEL_OPTS),
            "赠品": st.column_config.SelectboxColumn(options=list(GIFT_KINDS.keys())),
            "赠送数量": st.column_config.NumberColumn(min_value=1, step=1, width="small"),
            "买满几副": st.column_config.NumberColumn(min_value=1, step=1),
            "开始日期": st.column_config.DateColumn(format="MM-DD"),
            "结束日期": st.column_config.DateColumn(format="MM-DD"),
        })
    st.caption("⚠️ TikTok 的买 N 赠一指甲已在订单表里(自动计入)，别给 TikTok 配「指甲」赠品；"
               "独立站的免费赠指甲不在表里，在这里配置并核对实际买 N 赠 M 与日期后启用。")

# ---------- 第二步：上传 ----------
st.markdown("## ② 上传订单表（哪份有传哪份，最多 4 份）")
u1, u2 = st.columns(2)
u3, u4 = st.columns(2)
f_tt = u1.file_uploader("① TikTok 订单表（All order 导出）", type=["csv", "xlsx"], key="f_tt")
f_sp = u2.file_uploader("② 独立站订单表（Shopify 导出）", type=["csv", "xlsx"], key="f_sp")
f_dr = u3.file_uploader("③ 水单·深度达人单", type=["csv", "xlsx"], key="f_dr")
f_pt = u4.file_uploader("④ 水单·普通水单【客人+广达】", type=["csv", "xlsx"], key="f_pt")

jobs = [(f_tt, normalize_tiktok, "TikTok"), (f_sp, normalize_shopify, "独立站"),
        (f_dr, normalize_daren, "达人单"), (f_pt, normalize_putong, "普通水单")]
parts = []
for f, fn, label in jobs:
    if f is None:
        continue
    try:
        od = fn(read_any(f))
        parts.append(od)
        st.write(f"· {f.name} → **{label}**（{len(od)} 单）")
    except Exception as e:
        st.error(f"「{f.name}」按 {label} 解析失败：{e}")
        st.stop()

if not parts:
    st.info("👆 上传至少一份订单表开始计算。")
    st.stop()

od = pd.concat(parts, ignore_index=True)
od = od[~od["canceled"].fillna(False)].copy()
od = od[od["date"].notna()].copy()
if od.empty:
    st.error("没有可用订单（日期解析为空？请检查导出格式）。")
    st.stop()

metric_col = "subtotal" if metric_choice.startswith("商品小计") else "order_amount"
od = apply_rules_and_pack(od, spend_rules, buy_rules, metric_col, SMALL_MAX)

# ---------- 按日聚合 ----------
daily = od.groupby("date").agg(
    订单=("order_id", "count"), 赠折叠盒=("g_box", "sum"),
    大飞机袋=("big", "sum"), 小飞机袋=("small", "sum")).reset_index().sort_values("date")

max_d, min_d = daily["date"].max(), daily["date"].min()
w14 = daily[daily["date"] >= max_d - timedelta(days=13)]
w7 = daily[daily["date"] >= max_d - timedelta(days=6)]


def block(win, days, label):
    b, s, o = int(win["大飞机袋"].sum()), int(win["小飞机袋"].sum()), int(win["订单"].sum())
    st.markdown(f"#### {label}（{days} 天 · {o} 单）")
    a, c = st.columns(2)
    a.metric("🟦 大飞机袋 合计", f"{b:,} 个", f"日均 {b/days:.1f} 个/天")
    c.metric("🟨 小飞机袋 合计", f"{s:,} 个", f"日均 {s/days:.1f} 个/天")


st.markdown("## 📦 消耗汇总（全渠道合并）")
L, R = st.columns(2)
with L:
    block(w7, 7, "过去 7 天")
with R:
    block(w14, 14, "过去 14 天")
st.caption(f"数据日期：{min_d} ~ {max_d} · 门槛口径：{metric_choice} · "
           f"3 副→{'小袋' if SMALL_MAX==3 else '大袋'}")

# ---------- 分渠道 ----------
st.markdown("### 分渠道（全期）")
ch = od.groupby("channel").agg(订单=("order_id", "count"),
                               大飞机袋=("big", "sum"), 小飞机袋=("small", "sum")).reset_index()
st.dataframe(ch, use_container_width=True, hide_index=True)

# ---------- 赠品汇总 ----------
gb, gp, gd, gn = int(od["g_box"].sum()), int(od["g_pen"].sum()), int(od["g_binder"].sum()), int(od["g_nail"].sum())
if gb or gp or gd or gn:
    st.caption(f"🎁 按规则补加：折叠盒 {gb} · 卸甲笔 {gp} · 美甲册 {gd} · 赠指甲 {gn} 副")

# ---------- 图表 & 明细 ----------
st.markdown("## 📈 每日消耗")
st.bar_chart(daily.set_index("date")[["大飞机袋", "小飞机袋"]])
show = daily.copy(); show["date"] = show["date"].astype(str)
st.dataframe(show.rename(columns={"date": "日期"}), use_container_width=True, hide_index=True)

# ---------- 补货建议 ----------
st.markdown("## 🧮 补货建议")
big_avg = w14["大飞机袋"].sum() / 14
small_avg = w14["小飞机袋"].sum() / 14
normal = w14[w14["赠折叠盒"] == 0]
box_days = daily[daily["赠折叠盒"] > 0]
st.markdown(
    f"- **小飞机袋**：按 **≈ {small_avg:.0f} 个/天** 备货，基本不受促销影响。\n"
    f"- **大飞机袋**：平日 **≈ {(normal['大飞机袋'].mean() if len(normal) else big_avg):.0f} 个/天**。")
if len(box_days):
    st.warning("⚠️ **折叠盒赠品日单独加备**（每个赠盒占满一整个大袋）：\n\n" +
               "\n".join(f"- {r['date']}：大飞机袋 {int(r['大飞机袋'])} 个（赠盒 {int(r['赠折叠盒'])} 个）"
                         for _, r in box_days.iterrows()) +
               "\n\n补货公式：**当日大袋 ≈ 基础量 + 满额订单数 × 1**。")
st.caption("卸甲笔只占 2 位、多搭已有大袋，对大袋拉动很小；折叠盒才是大袋的主要放大器。"
           "达人批量发货日(如整批达人单)会有小尖峰，可留意。")

with st.expander("📖 计算口径与假设"):
    st.markdown("""
- **只排除取消单**；**零元单全部保留**。
- **TikTok**：1 Order ID = 1 单，指甲 SKU 行数 = 副数；免费赠指甲($0 行)已在表内自动计入。
- **独立站**：1 订单号 = 1 单，指甲 Lineitem 行数 = 副数；免费赠指甲不在表内，用「买副赠·指甲(独立站)」补。
- **深度达人单 / 普通水单**：1 handle(或 Order ID) = 1 单，款式名数(逗号分隔) = 副数；发货清单已含实发赠品，**不再叠加赠品**。款式名里若出现 storage box/binder/kit/remover/pen 等**配件名**，按占位规则算(折叠盒占满大袋、册4位、笔/kit/remover 2位)、不当指甲副。
- **配件占位**：折叠盒 6 位(占满一个大袋)、美甲册 4 位、卸甲笔 2 位、Kit/Toolkit 按 2 位。
- **规则③**：3 副→大袋 时小袋装 1–2 副；3 副→小袋 时小袋装 1–3 副（同时影响 >6 副的余数判断）。
- **过去 7/14 天**：以表内最后一天为基准往前推 7 / 14 个自然日；日均 = 合计 ÷ 7(或 14)。
    """)
