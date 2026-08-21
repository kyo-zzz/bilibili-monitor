"""每周/每月柱状图可视化: matplotlib 输出 PNG, 并生成可自动刷新的 index.html 索引页."""
import glob
import html
import logging
import os
import unicodedata
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Patch

from .config import account_labels
from .util import fmt_num

log = logging.getLogger("bmon.charts")

TSFMT = "%Y-%m-%d %H:%M:%S"
ACCENT = ["#00A1D6", "#F25D8E", "#7B5FFF", "#2ECC71", "#F39C12",
          "#E74C3C", "#1ABC9C", "#9B59B6", "#5D6D7E", "#16A085"]


def setup_font(prefer=""):
    """探测并注册中文字体, 避免图表中文乱码."""
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "C:/Windows/Fonts/msyhbd.ttc"):
        if os.path.exists(f):
            try:
                font_manager.fontManager.addfont(f)
            except Exception:
                pass
    avail = {f.name for f in font_manager.fontManager.ttflist}
    for name in ([prefer] if prefer else []) + [
            "Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
            "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS"]:
        if name and name in avail:
            plt.rcParams["font.sans-serif"] = [name]
            plt.rcParams["axes.unicode_minus"] = False
            return name
    plt.rcParams["axes.unicode_minus"] = False
    return None


# ---------- 周期划分 ----------
def week_start(d):
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(d):
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(s):
    return month_start(s.replace(day=28) + timedelta(days=5))


def build_periods(kind, n, now=None):
    """返回 [(标签, 起始datetime, 结束datetime)], 时间升序, 含当前周期."""
    now = now or datetime.now()
    n = max(1, int(n))
    periods = []
    if kind == "weekly":
        cur = week_start(now)
        for i in range(n - 1, -1, -1):
            s = cur - timedelta(weeks=i)
            iso = s.isocalendar()
            periods.append((f"{iso[0]}-W{iso[1]:02d}", s, s + timedelta(weeks=1)))
    elif kind == "monthly":
        starts, s = [], month_start(now)
        for _ in range(n):
            starts.append(s)
            s = month_start(s - timedelta(days=1))
        for s in reversed(starts):
            periods.append((s.strftime("%Y-%m"), s, _next_month(s)))
    else:
        raise ValueError(f"未知周期类型: {kind}")
    return periods


# ---------- 账号样式 ----------
def account_style(cfg, rows):
    """按配置顺序为账号分配稳定的显示名与颜色."""
    labels = dict(account_labels(cfg))
    ordered = list(labels.keys())
    for r in rows:
        m = r.get("mid")
        if m not in labels:
            labels[m] = r.get("account") or str(m)
            ordered.append(m)
    present = {r.get("mid") for r in rows}
    ordered = [m for m in ordered if m in present]
    colors = {m: ACCENT[i % len(ACCENT)] for i, m in enumerate(ordered)}
    return ordered, labels, colors


# ---------- 聚合 ----------
def agg_published(rows, periods):
    counts = {}
    for r in rows:
        ts = r.get("created_ts")
        if not ts:
            continue
        d = datetime.fromtimestamp(ts)
        for i, (_, s, e) in enumerate(periods):
            if s <= d < e:
                counts.setdefault(r["mid"], [0] * len(periods))[i] += 1
                break
    return counts


def agg_gains(db, periods, rows):
    """按快照差值计算各账号每期播放增量; 同时返回最近一期增长Top视频.

    基线取该期开始前最后一个快照; 若监控在中途才开始, 则以窗口内首个快照为基线
    (即只统计观测期内的增量).
    """
    bvid_row = {r["bvid"]: r for r in rows}
    if not bvid_row:
        return {}, []
    start = periods[0][1].strftime(TSFMT)
    end = periods[-1][2].strftime(TSFMT)
    snaps = db.snapshots_between(start, end, bvids=set(bvid_row))
    series = {}
    for s in snaps:
        if s["view"] is None:
            continue
        series.setdefault(s["bvid"], []).append((s["ts"], s["view"]))  # 已按ts升序

    gains = {}
    latest_top = []
    last_idx = len(periods) - 1
    for bvid, ser in series.items():
        mid = bvid_row[bvid]["mid"]
        bucket = gains.setdefault(mid, [0] * len(periods))
        for i, (_, s, e) in enumerate(periods):
            ss, ee = s.strftime(TSFMT), e.strftime(TSFMT)
            in_p = [(t, v) for t, v in ser if ss <= t < ee]
            if not in_p:
                continue
            end_v = in_p[-1][1]
            before = [(t, v) for t, v in ser if t < ss]
            base_v = before[-1][1] if before else in_p[0][1]
            g = max(0, end_v - base_v)
            bucket[i] += g
            if i == last_idx and g > 0:
                latest_top.append((g, bvid_row[bvid]))
    latest_top.sort(key=lambda x: -x[0])
    return gains, latest_top


# ---------- 绘图元件 ----------
def _wrap_lines(title, width):
    """按显示宽度折行, 返回行列表(不丢字)."""
    def cw(ch):
        return 2 if unicodedata.east_asian_width(ch) in "FW" else 1
    lines, cur, w = [], "", 0
    for ch in str(title).strip():
        c = cw(ch)
        if w + c > width:
            lines.append(cur)
            cur, w = ch, c
        else:
            cur += ch
            w += c
    if cur:
        lines.append(cur)
    return lines


def _wrap_title(title, max_lines=3):
    """把标题折成不超过 max_lines 行, 完整保留全部文字(不截断);
    长标题自动放宽行宽重折, 宁可宽一点也不丢字."""
    lines = []
    for width in (30, 36, 42, 50, 60):
        lines = _wrap_lines(title, width)
        if len(lines) <= max_lines:
            break
    return "\n".join(lines)


def _style_axes(ax):
    """统一现代化样式: 去上/右边框, 虚线网格, 柔和刻度."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c8c8c8")
    ax.tick_params(colors="#555", labelsize=9.5)
    ax.grid(axis="y", alpha=0.35, linestyle="--", linewidth=0.7)
    ax.set_axisbelow(True)


def _grouped(ax, period_labels, series, colors, labels, title, ylabel):
    n = max(1, len(series))
    m = len(period_labels)
    x = list(range(m))
    width = 0.8 / n
    rotate = m * n > 16
    for i, (mid, vals) in enumerate(series.items()):
        pos = [xi + (i - (n - 1) / 2) * width for xi in x]
        bars = ax.bar(pos, vals, width=width * 0.95, label=labels.get(mid, mid),
                      color=colors.get(mid, "#999"), edgecolor="white",
                      linewidth=0.6, zorder=3)
        ax.bar_label(bars, labels=[fmt_num(v) if v else "" for v in vals],
                     fontsize=8, rotation=90 if rotate else 0, padding=2,
                     color="#3a3a3a")
    ax.set_xticks(x)
    ax.set_xticklabels(period_labels, rotation=45, ha="right", fontsize=9)
    ax.set_title(title, fontsize=13, loc="left", pad=12, color="#222")
    ax.set_ylabel(ylabel, color="#555")
    ax.legend(fontsize=9, frameon=False)
    ax.grid(axis="y", alpha=0.35, linestyle="--", linewidth=0.7)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c8c8c8")
    ax.tick_params(colors="#555")
    if rotate:
        ax.margins(y=0.15)


def _top(ax, rows, n, colors, labels, title, key=None, note="暂无数据"):
    """横向Top榜: 仅用标题做条目标签(完整折行不截断), 游戏以颜色+图例区分;
    长条数值放条内白字, 短条放条外, 前三名金色强调, 左侧标注名次."""
    key = key or (lambda r: r.get("latest_view"))
    items = [r for r in rows if key(r) is not None]
    items.sort(key=key, reverse=True)
    items = items[:n]
    if not items:
        ax.text(0.5, 0.5, note, ha="center", va="center",
                transform=ax.transAxes, color="#888", fontsize=12)
        ax.set_title(title, fontsize=13, loc="left", pad=12, color="#222")
        ax.set_xticks([])
        ax.set_yticks([])
        return
    items.reverse()                      # 最大值排最上
    names = [_wrap_title(r.get("title", "")) for r in items]
    max_lines = max(s.count("\n") + 1 for s in names)
    fsize = 9 if max_lines <= 2 else (8.4 if max_lines == 3 else 7.8)
    vals = [key(r) for r in items]
    vmax = max(vals) or 1
    present = []
    for r in items:
        if r.get("mid") not in present:
            present.append(r.get("mid"))
    ys = list(range(len(items)))
    ax.barh(ys, vals, height=0.72,
            color=[colors.get(r.get("mid"), "#999") for r in items],
            edgecolor="white", linewidth=0.6, zorder=3)
    ax.set_yticks(ys)
    ax.set_yticklabels(names, fontsize=fsize, color="#333", linespacing=1.2)
    for i, (y, v) in enumerate(zip(ys, vals)):
        rank = len(items) - i
        ax.text(-vmax * 0.013, y, f"{rank:02d}", ha="right", va="center",
                fontsize=7.8, color="#b8b8b8")
        if v >= vmax * 0.55:
            ax.text(v - vmax * 0.013, y, fmt_num(v), ha="right", va="center",
                    color="white", fontsize=8.6, fontweight="bold", zorder=4)
        else:
            ax.text(v + vmax * 0.016, y, fmt_num(v), ha="left", va="center",
                    color="#c98a00" if rank <= 3 else "#3a3a3a",
                    fontsize=8.8, fontweight="bold" if rank <= 3 else "normal",
                    zorder=4)
    ax.set_xlim(-vmax * 0.05, vmax * 1.13)
    ax.set_title(title, fontsize=13, loc="left", pad=12, color="#222")
    ax.set_xticks([])
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#c8c8c8")
    ax.tick_params(colors="#555", length=0)
    ax.grid(axis="x", alpha=0.3, linestyle="--", linewidth=0.7)
    ax.set_axisbelow(True)
    ax.legend(handles=[Patch(color=colors.get(m, "#999"), label=labels.get(m, m))
                       for m in present],
              loc="lower right", ncols=len(present), fontsize=9, frameon=False)


def _save(fig, cfg, filename):
    outdir = cfg["charts"]["output_dir"]
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, filename)
    fig.savefig(path)
    plt.close(fig)
    return path


# ---------- 对外入口 ----------
def make_dashboard(db, cfg, kind, rows):
    cc = cfg["charts"]
    n_back = int(cc.get("periods_back", 12))
    top_n = int(cc.get("top_n", 15))
    periods = build_periods(kind, n_back)
    period_labels = [p[0] for p in periods]
    ordered, labels, colors = account_style(cfg, rows)

    counts = agg_published(rows, periods)
    gains, latest_top = agg_gains(db, periods, rows)

    win_start = periods[0][1]
    win_rows = [r for r in rows if r.get("created_ts")
                and datetime.fromtimestamp(r["created_ts"]) >= win_start]
    unit = "周" if kind == "weekly" else "个月"

    fig, axes = plt.subplots(2, 2, figsize=(17.5, 13), dpi=140)
    fig.patch.set_facecolor("white")
    fig.suptitle(
        f"B站官号视频数据{unit}报 · {period_labels[0]} ~ {period_labels[-1]}"
        f" · 生成于 {datetime.now():%Y-%m-%d %H:%M}",
        fontsize=16, fontweight="bold", color="#111")

    _grouped(axes[0][0], period_labels,
             {m: counts.get(m, [0] * len(periods)) for m in ordered},
             colors, labels, f"每{unit}新发布视频数", "视频数")

    g_series = {m: gains.get(m, [0] * len(periods)) for m in ordered}
    _grouped(axes[0][1], period_labels, g_series, colors, labels,
             f"每{unit}新增播放量(快照差值)", "播放增量")
    if not ordered or all(sum(v) == 0 for v in g_series.values()):
        axes[0][1].text(0.5, 0.55, "暂无增长数据\n需系统持续运行积累快照",
                        ha="center", va="center", transform=axes[0][1].transAxes,
                        color="#c00", fontsize=12)

    _top(axes[1][0], win_rows, top_n, colors, labels,
         f"近{n_back}{unit}发布视频 · 累计播放 Top{top_n}")

    gain_rows = [dict(r, latest_view=g) for g, r in latest_top]
    _top(axes[1][1], gain_rows, min(10, top_n), colors, labels,
         f"本期({period_labels[-1]})播放增长 Top{min(10, top_n)}",
         note="本期暂无增长数据")

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return _save(fig, cfg, f"dashboard_{kind}_{period_labels[-1]}.png")


def make_single(db, cfg, kind, ctype, rows):
    cc = cfg["charts"]
    n_back = int(cc.get("periods_back", 12))
    top_n = int(cc.get("top_n", 15))
    periods = build_periods(kind, n_back)
    period_labels = [p[0] for p in periods]
    ordered, labels, colors = account_style(cfg, rows)
    unit = "周" if kind == "weekly" else "个月"

    if ctype in ("published", "gained"):
        fig, ax = plt.subplots(figsize=(14.5, 7.5), dpi=140)
    else:
        fig, ax = plt.subplots(figsize=(15, 10), dpi=140)
    fig.patch.set_facecolor("white")
    if ctype == "published":
        counts = agg_published(rows, periods)
        _grouped(ax, period_labels,
                 {m: counts.get(m, [0] * len(periods)) for m in ordered},
                 colors, labels, f"每{unit}新发布视频数", "视频数")
    elif ctype == "gained":
        gains, _ = agg_gains(db, periods, rows)
        _grouped(ax, period_labels,
                 {m: gains.get(m, [0] * len(periods)) for m in ordered},
                 colors, labels, f"每{unit}新增播放量(快照差值)", "播放增量")
    elif ctype == "top":
        win_start = periods[0][1]
        win_rows = [r for r in rows if r.get("created_ts")
                    and datetime.fromtimestamp(r["created_ts"]) >= win_start]
        _top(ax, win_rows, top_n, colors, labels,
             f"近{n_back}{unit}发布视频 · 累计播放 Top{top_n}")
    else:
        raise ValueError(f"未知图表类型: {ctype}")

    fig.tight_layout()
    return _save(fig, cfg, f"{ctype}_{kind}_{period_labels[-1]}.png")


def write_index(cfg):
    outdir = cfg["charts"]["output_dir"]
    os.makedirs(outdir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(outdir, "*.png")),
                   key=os.path.getmtime, reverse=True)[:60]
    ref = int(cfg["charts"].get("auto_refresh_seconds") or 0)
    meta = f'<meta http-equiv="refresh" content="{ref}">' if ref > 0 else ""
    items = "".join(
        f'<figure><img src="{html.escape(os.path.basename(f))}" loading="lazy">'
        f'<figcaption>{html.escape(os.path.basename(f)[:-4].replace("_", " · "))}</figcaption></figure>'
        for f in files)
    doc = (
        "<!doctype html><html><head><meta charset=\"utf-8\">" + meta +
        "<title>B站官号视频数据监测</title><style>"
        "body{font-family:'Microsoft YaHei',sans-serif;background:#101418;color:#e8e8e8;"
        "margin:24px;max-width:1500px}"
        "h1{font-size:20px}p{color:#889}"
        "figure{margin:0 0 36px}img{max-width:100%;border-radius:8px;background:#fff}"
        "figcaption{color:#778;font-size:13px;margin-top:6px}"
        "</style></head><body>"
        "<h1>B站官号视频数据监测 · 自动图表</h1>"
        f"<p>共 {len(files)} 张 · 由监测系统自动更新"
        + (" · 页面每 " + str(ref) + " 秒自动刷新" if ref > 0 else "")
        + "</p>" + items + "</body></html>")
    path = os.path.join(outdir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
