# -*- coding: utf-8 -*-
"""
NailVesta 飞机袋消耗测算（4 来源：TikTok + 独立站 + 深度达人单 + 普通水单）
先手动配置规则 → 上传订单表 → 自动算「过去 7 天 / 过去 14 天」的小/大飞机袋、飞机盒、工具包日均，
并给出每单件数分布供复核。

手动规则：
  ① 满多少钱送什么（满额赠）
  ② 买几副送什么（买副赠，独立站赠指甲用它补）
  ③ 袋子自带工具包数（小袋 0~3、大袋 0~10，默认小 1、大 3；缺大袋改自购时把大袋设 0）

包装规则（按订单日期自动切换 · 纯按指甲件数分档）：
  1~2 副 → 1 个小飞机袋（两个时段一致）。
  ≤ 9/14（旧）：3~10 副 → 1 个大飞机袋；>10 副 → 飞机盒。
  ≥ 9/15（新 · 以后默认）：3~8 副 → 1 个大飞机袋；≥9 副 → 飞机盒。
  装盒：指甲先按当期容量(旧 10/新 8)拆成多个大飞机袋(向上取整)，大飞机袋每 2 个装进 1 个飞机盒(向上取整)。
  工具包消耗：每个袋内 max(0, 袋内件数−该袋自带) 之和(装盒单逐袋算)；飞机盒只是外层容器、本身不带工具包。
  不再按配件占位换袋——配件名只是「不计入指甲件数」。

数据口径：
  · TikTok：1 Order ID = 1 单；指甲 SKU 行数 = 件数。免费赠指甲($0 行)已在表内自动计入。
  · 独立站：1 订单号 = 1 单；指甲 Lineitem 行数 = 件数。免费赠指甲不在表内 → 用「买副赠·指甲」补。
  · 深度达人单 / 普通水单(合并「水单」)：1 handle(或 Order ID) = 1 单；款式名数(逗号分隔) = 件数。
  · 只排除取消单，零元单全部保留。水单是发货清单、不叠加赠品。
"""

import re
from datetime import date, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="NailVesta 飞机袋消耗测算", page_icon="✈️", layout="wide")

# ============ 物料 / 打包配置 ============
SMALL_MAX = 2       # 1~2 副 → 小飞机袋（两个时段一致）
BOX_BAGS = 2        # 一个飞机盒最多装 2 个大飞机袋
STRATEGY_CUTOFF = date(2026, 9, 15)  # 打包策略切换日：≥9/15 用新策略、之前用旧策略
BAG_CAP_OLD = 10    # 9/15 前：大飞机袋每袋 ≤10 副（3~10 副 1 袋，>10 装盒）
BAG_CAP_NEW = 8     # 9/15 起：大飞机袋每袋 ≤8 副（3~8 副 1 袋，≥9 装盒）


def bag_cap_for(d):
    """按订单日期取当期大飞机袋每袋容量：≥9/15 用新策略(8)、之前旧策略(10)。"""
    return BAG_CAP_NEW if d >= STRATEGY_CUTOFF else BAG_CAP_OLD

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


def big_bag_split(n, cap):
    """走大飞机袋时把 n 副按每袋最多 cap 副拆分，返回各袋副数列表(最后一袋装余数)。"""
    bags = []
    while n > 0:
        bags.append(min(cap, n))
        n -= cap
    return bags


def package_of(n_nails, cap):
    """按指甲件数定包装，返回 (小飞机袋, 大飞机袋, 飞机盒) 的个数。cap=当期大飞机袋容量。
    1~2 副 → 1 小袋；3~cap 副 → 1 大袋；
    >cap 副 → 每 cap 副 1 大袋(向上取整)，大飞机袋每 2 个装进 1 个飞机盒(向上取整)。"""
    n = int(round(n_nails))
    if n <= 0:
        return 0, 0, 0
    if n <= SMALL_MAX:
        return 1, 0, 0                              # 小飞机袋
    big = len(big_bag_split(n, cap))                # 每袋≤cap 副
    box = -(-big // BOX_BAGS) if big > 1 else 0     # 2 个大袋装 1 盒；单个大袋不装盒
    return 0, big, box                              # 大飞机袋×big（装盒时 box≥1）


def toolkit_of(n_nails, small_builtin, big_builtin, cap):
    """工具包消耗 = 每个袋子内 max(0, 袋内指甲件数 − 该袋自带数) 之和。
    小袋按 small_builtin；大袋按 big_builtin（>cap 件拆多袋、逐袋算）。
    飞机盒只是外层容器，工具包都在里面的大飞机袋里算。"""
    n = int(round(n_nails))
    if n <= 0:
        return 0
    if n <= SMALL_MAX:
        return max(0, n - small_builtin)
    return sum(max(0, k - big_builtin) for k in big_bag_split(n, cap))


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
    """深度达人单：Handle=订单，款式(逗号分隔)=款式；配件名按占位算。
    渠道统一记「水单」——达人单与客人单合并统计，不再分开。"""
    return _build_waterslip(df, "Handle", "款式", "水单")


def normalize_putong(df):
    """普通水单【客人+广达】：Order ID=订单，Product Name(逗号分隔)=款式；配件名按占位算。
    渠道统一记「水单」——与深度达人单合并统计，不再分开。"""
    return _build_waterslip(df, "Order ID", "Product Name", "水单")


def read_any(file):
    if file.name.lower().endswith(".xlsx"):
        return pd.read_excel(file, dtype=str)
    return pd.read_csv(file, dtype=str, encoding="utf-8-sig")


def apply_rules_and_pack(od, spend_rules, buy_rules, metric_col):
    """对合并后的订单级表应用赠品规则(仅 TK/独立站)并装袋，返回带 big/small 的表。
    装袋按每单订单日期切换策略(bag_cap_for)：≤9/14 大袋≤10 副、≥9/15 大袋≤8 副。"""
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

    n = od["n_nails"].round().astype(int)
    od["件数"] = n
    od["cap"] = od["date"].map(bag_cap_for)         # 每单按订单日期取当期大袋容量
    pk = [package_of(x, c) for x, c in zip(n, od["cap"])]
    od["small"] = [t[0] for t in pk]                # 小飞机袋(个数)
    od["big"] = [t[1] for t in pk]                  # 大飞机袋(个数，装盒单会 >1)
    od["box"] = [t[2] for t in pk]                  # 飞机盒(个数)
    return od


# ============ 侧边栏：口径 ============
st.sidebar.header("⚙️ 计算口径")
metric_choice = st.sidebar.radio(
    "满额门槛看哪个金额（实付）",
    ["商品小计（不含运费税）", "订单实付总额（含运费税）"], index=0)
st.sidebar.caption("只排除取消单；零元单全部保留。水单不叠加赠品(发货清单已含实发)。")

# ============ 主体 ============
st.title("✈️ NailVesta 飞机袋消耗测算")
st.caption("① 先配置规则(4 项) → ② 上传订单表(最多 4 份) → 自动算过去 7 天 / 14 天日均。")

# ---------- 第一步：规则 ----------
st.markdown("## ① 规则设置")

st.markdown("**打包策略：按订单日期自动切换**（小袋固定 1~2 副；装盒每 2 个大飞机袋 1 盒）")
st.markdown(
    f"- **≤ 9/14（旧）**：1~2 副 → 小飞机袋；3~{BAG_CAP_OLD} 副 → 大飞机袋；>{BAG_CAP_OLD} 副 → 飞机盒。\n"
    f"- **≥ 9/15（新 · 以后默认）**：1~2 副 → 小飞机袋；3~{BAG_CAP_NEW} 副 → 大飞机袋；≥{BAG_CAP_NEW+1} 副 → 飞机盒。\n"
    f"- **装盒**：指甲先按当期容量(旧 {BAG_CAP_OLD}/新 {BAG_CAP_NEW})拆成多个大飞机袋，大飞机袋每 {BOX_BAGS} 个装进 1 个飞机盒。")

st.markdown("**规则③：袋子自带工具包数**（手动选；缺大袋、改用自购大袋时把大袋改成 0）")
bt1, bt2, _ = st.columns([1, 1, 2])
SMALL_BUILTIN = bt1.number_input("小飞机袋自带", min_value=0, max_value=3, value=1, step=1)
BIG_BUILTIN = bt2.number_input("大飞机袋自带", min_value=0, max_value=10, value=3, step=1)
st.caption(f"工具包消耗 = 每个袋内(指甲件数 − 该袋自带数，不足记 0)之和；装盒的单拆多个大飞机袋逐袋算，飞机盒本身不带工具包。"
           f"当前：小袋自带 {SMALL_BUILTIN}、大袋自带 {BIG_BUILTIN}（下面按 9/15 起策略·每袋≤{BAG_CAP_NEW} 副举例）："
           f"1件→{toolkit_of(1,SMALL_BUILTIN,BIG_BUILTIN,BAG_CAP_NEW)}、"
           f"3件→{toolkit_of(3,SMALL_BUILTIN,BIG_BUILTIN,BAG_CAP_NEW)}、"
           f"8件→{toolkit_of(8,SMALL_BUILTIN,BIG_BUILTIN,BAG_CAP_NEW)}、"
           f"9件(2袋)→{toolkit_of(9,SMALL_BUILTIN,BIG_BUILTIN,BAG_CAP_NEW)}、"
           f"16件(2袋)→{toolkit_of(16,SMALL_BUILTIN,BIG_BUILTIN,BAG_CAP_NEW)}、"
           f"17件(3袋)→{toolkit_of(17,SMALL_BUILTIN,BIG_BUILTIN,BAG_CAP_NEW)}。")

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
        (f_dr, normalize_daren, "水单·深度达人"), (f_pt, normalize_putong, "水单·普通")]
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
od = apply_rules_and_pack(od, spend_rules, buy_rules, metric_col)
od["toolkit"] = [toolkit_of(x, SMALL_BUILTIN, BIG_BUILTIN, c)
                 for x, c in zip(od["n_nails"], od["cap"])]

# ---------- 按日聚合 ----------
daily = od.groupby("date").agg(
    订单=("order_id", "count"),
    小飞机袋=("small", "sum"), 大飞机袋=("big", "sum"), 飞机盒=("box", "sum"),
    工具包=("toolkit", "sum")).reset_index().sort_values("date")

max_d, min_d = daily["date"].max(), daily["date"].min()
w14 = daily[daily["date"] >= max_d - timedelta(days=13)]
w7 = daily[daily["date"] >= max_d - timedelta(days=6)]


def block(win, days, label):
    s, b, x = int(win["小飞机袋"].sum()), int(win["大飞机袋"].sum()), int(win["飞机盒"].sum())
    k, o = int(win["工具包"].sum()), int(win["订单"].sum())
    st.markdown(f"#### {label}（{days} 天 · {o} 单）")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🟨 小飞机袋", f"{s:,}", f"日均 {s/days:.1f}")
    c2.metric("🟦 大飞机袋", f"{b:,}", f"日均 {b/days:.1f}")
    c3.metric("📦 飞机盒", f"{x:,}", f"日均 {x/days:.1f}")
    c4.metric("🧰 工具包", f"{k:,}", f"日均 {k/days:.1f}")


st.markdown("## 📦 消耗汇总（全渠道合并）")
block(w7, 7, "过去 7 天")
block(w14, 14, "过去 14 天")
st.caption(f"数据日期：{min_d} ~ {max_d} · 门槛口径：{metric_choice} · "
           f"分档(按订单日期)：≤9/14 小袋1~2/大袋3~{BAG_CAP_OLD}/飞机盒>{BAG_CAP_OLD}；"
           f"≥9/15 小袋1~2/大袋3~{BAG_CAP_NEW}/飞机盒≥{BAG_CAP_NEW+1}；装盒每 {BOX_BAGS} 袋 1 盒 · "
           f"自带工具包 小{SMALL_BUILTIN}/大{BIG_BUILTIN}")

# ---------- 分渠道 ----------
st.markdown("### 分渠道（全期）")
ch = od.groupby("channel").agg(订单=("order_id", "count"),
                               小飞机袋=("small", "sum"), 大飞机袋=("big", "sum"),
                               飞机盒=("box", "sum"), 工具包=("toolkit", "sum")).reset_index()
st.dataframe(ch, use_container_width=True, hide_index=True)

# ---------- 购买件数分布（逐件 · 从买 1 件到最多件） ----------
st.markdown("## 🔍 购买件数分布（每个包裹买了几件 · 逐件统计）")
o7 = od[od["date"] >= max_d - timedelta(days=6)]
o14 = od[od["date"] >= max_d - timedelta(days=13)]
vc7, vc14 = o7["件数"].value_counts(), o14["件数"].value_counts()
max_n = int(o14["件数"].max()) if len(o14) else 0     # 近 14 天里最多买了几件


def pkg_label(k):
    """该购买件数走哪种包装（按分档）；9~10 件按订单日期不同。"""
    if k <= 0:
        return "—"
    if k <= SMALL_MAX:
        return "小飞机袋"
    if k <= BAG_CAP_NEW:                  # 3~8 副：两期都是 1 个大飞机袋
        return "大飞机袋"
    if k <= BAG_CAP_OLD:                  # 9~10 副：≤9/14 大飞机袋、≥9/15 飞机盒
        return "≤9/14 大袋 · ≥9/15 盒"
    return "飞机盒"                        # ≥11 副：两期都装盒


dist_rows = [{"购买件数": f"{k} 件", "包装": pkg_label(k),
              "近7天(单)": int(vc7.get(k, 0)), "近14天(单)": int(vc14.get(k, 0))}
             for k in range(1, max_n + 1)]
dist_rows.append({"购买件数": "0 件(无指甲)", "包装": "—",
                  "近7天(单)": int(vc7.get(0, 0)), "近14天(单)": int(vc14.get(0, 0))})
dist_rows.append({"购买件数": "合计", "包装": "",
                  "近7天(单)": len(o7), "近14天(单)": len(o14)})
st.dataframe(pd.DataFrame(dist_rows), use_container_width=True, hide_index=True)
box7, box14 = int(o7["box"].sum()), int(o14["box"].sum())
bag7, bag14 = int(o7["big"].sum()), int(o14["big"].sum())
st.caption(f"每行 = 恰好买 N 件指甲的订单数（件数 = 指甲副数，不含配件）。近 14 天最多买 {max_n} 件。"
           f"实际消耗（近7/近14天）：大飞机袋 {bag7}/{bag14} 个、**飞机盒 {box7}/{box14} 个**（已计入上方汇总）；"
           f"装盒的单把指甲拆成多个大飞机袋(每袋≤当期容量)、再每 {BOX_BAGS} 袋装 1 盒。")

# ---------- 工具包消耗（袋子自带外的额外用量） ----------
st.markdown(f"## 🧰 工具包消耗（小袋自带 {SMALL_BUILTIN}、大袋自带 {BIG_BUILTIN}，只记超出部分）")
present = [c for c in ["TikTok", "独立站", "水单"] if c in set(od["channel"])]


def kit_sum(days):
    w = od[od["date"] >= max_d - timedelta(days=days - 1)]
    return w.groupby("channel")["toolkit"].sum()


k7, k14 = kit_sum(7), kit_sum(14)
kit_tbl = pd.DataFrame({
    "渠道": present,
    "近7天": [int(k7.get(c, 0)) for c in present],
    "近14天": [int(k14.get(c, 0)) for c in present],
})
kit_tbl["日均(近14天)"] = (kit_tbl["近14天"] / 14).round(1)
kit_tbl.loc[len(kit_tbl)] = ["合计", int(kit_tbl["近7天"].sum()),
                             int(kit_tbl["近14天"].sum()),
                             round(kit_tbl["近14天"].sum() / 14, 1)]
st.dataframe(kit_tbl, use_container_width=True, hide_index=True)
st.caption(f"工具包消耗 = 小袋 max(0, 件数−{SMALL_BUILTIN})、大袋 max(0, 袋内件数−{BIG_BUILTIN})；装盒的单拆多个大飞机袋逐袋算(飞机盒本身不额外带工具包)。"
           "独立站的赠指甲需在「规则②·买副赠·指甲」配置并启用后才会计入。")

# ---------- 赠品汇总 ----------
gb, gp, gd, gn = int(od["g_box"].sum()), int(od["g_pen"].sum()), int(od["g_binder"].sum()), int(od["g_nail"].sum())
if gb or gp or gd or gn:
    st.caption(f"🎁 按规则补加：折叠盒 {gb} · 卸甲笔 {gp} · 美甲册 {gd} · 赠指甲 {gn} 副")

# ---------- 图表 & 明细 ----------
st.markdown("## 📈 每日消耗")
st.bar_chart(daily.set_index("date")[["小飞机袋", "大飞机袋", "飞机盒"]])
show = daily.copy(); show["date"] = show["date"].astype(str)
st.dataframe(show.rename(columns={"date": "日期"}), use_container_width=True, hide_index=True)

# ---------- 补货建议 ----------
st.markdown("## 🧮 补货建议")
small_avg = w14["小飞机袋"].sum() / 14
big_avg = w14["大飞机袋"].sum() / 14
box_avg = w14["飞机盒"].sum() / 14
kit_avg = w14["工具包"].sum() / 14
st.markdown(
    f"- **小飞机袋**（1~2 件）：按 **≈ {small_avg:.0f} 个/天** 备货。\n"
    f"- **大飞机袋**（含装盒单拆出的多个大袋）：按 **≈ {big_avg:.0f} 个/天** 备货。\n"
    f"- **飞机盒**（≤9/14 >{BAG_CAP_OLD} 副、≥9/15 ≥{BAG_CAP_NEW+1} 副；每盒 {BOX_BAGS} 袋）：按 **≈ {box_avg:.1f} 个/天** 备货。\n"
    f"- **工具包**：按 **≈ {kit_avg:.0f} 个/天** 备货（小袋自带 {SMALL_BUILTIN}、大袋自带 {BIG_BUILTIN}；装盒单逐袋算）。")
st.caption("飞机盒装大飞机袋、每盒 2 袋；达人批量发货日会有小尖峰，可留意。")

with st.expander("📖 计算口径与假设"):
    st.markdown("""
- **只排除取消单**；**零元单全部保留**。
- **TikTok**：1 Order ID = 1 单，指甲 SKU 行数 = 副数；免费赠指甲($0 行)已在表内自动计入。
- **独立站**：1 订单号 = 1 单，指甲 Lineitem 行数 = 副数；免费赠指甲不在表内，用「买副赠·指甲(独立站)」补。
- **水单（深度达人单 + 普通水单【客人+广达】合并为「水单」）**：1 handle(或 Order ID) = 1 单，款式名数(逗号分隔) = 件数；发货清单已含实发赠品，不叠加赠品。款式名里的 storage box/binder/kit/remover/pen 等**配件名**只是**不计入指甲件数**（不当指甲），不再影响袋型。
- **包装（按订单日期自动切换）**：1~2 副 → 1 个小飞机袋（两个时段一致）。**≤ 9/14（旧）**：3~10 副 → 1 个大飞机袋；>10 副 → 飞机盒。**≥ 9/15（新 · 以后默认）**：3~8 副 → 1 个大飞机袋；≥9 副 → 飞机盒。**装盒**：指甲先按当期容量(旧 10/新 8)拆成多个大飞机袋(向上取整)，大飞机袋每 2 个装进 1 个飞机盒(向上取整)。例(新策略)：9 副=2 袋 1 盒、16 副=2 袋 1 盒、17 副=3 袋 2 盒。**不再按配件占位换袋。**
- **工具包**：每个袋内 max(0, 袋内件数 − 该袋自带) 之和；小袋按小袋自带、大袋按大袋自带；**飞机盒只是外层容器、本身不带工具包**（装盒的单拆成多个大飞机袋后逐袋算）。规则③手动设自带数(小袋 0~3、大袋 0~10，默认小 1、大 3；缺大袋改自购时把大袋设 0)。例(小1/大3 · 新策略每袋≤8)：1件→0、3件→0、8件→5、9件(2袋)→5、16件(2袋)→10、17件(3袋)→10。独立站赠指甲要计入需在「规则②·买副赠·指甲」配置并启用。
- **件数分布表**：近 7/14 天各件数(1/2/3~8/9~10/11+)的包裹数，用于复核；9~10 副按订单日期分档不同(≤9/14 大飞机袋、≥9/15 飞机盒)，实际大飞机袋/飞机盒个数见表下说明与上方汇总。
- **过去 7/14 天**：以表内最后一天为基准往前推 7 / 14 个自然日；日均 = 合计 ÷ 7(或 14)。
    """)
