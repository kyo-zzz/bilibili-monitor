"""每周/每月柱状图可视化: matplotlib 输出 PNG, 并生成可自动刷新的 index.html 索引页."""
import glob
import html
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Patch, Rectangle

from .config import account_labels
from .util import fmt_num

log = logging.getLogger("bmon.charts")

TSFMT = "%Y-%m-%d %H:%M:%S"
# 纸感平设色板(与 Web UI "Paper Editorial" 同源)
PAPER, INK, DIM_C, GRID_C = "#f7f6f0", "#1a1c22", "#7c818e", "#e4e2d6"
CORAL, BUTTER, LAVENDER, MINT = "#ff5a5a", "#f5b93f", "#8f7bff", "#2fbf71"
ACCENT = [CORAL, BUTTER, LAVENDER, MINT, "#3aa4d6", "#e8729e",
          "#8bc34a", "#f4863a", "#5d6d7e", "#16a085"]


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
    if kind == "daily":
        cur = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(n - 1, -1, -1):
            s = cur - timedelta(days=i)
            periods.append((s.strftime("%m-%d"), s, s + timedelta(days=1)))
    elif kind == "weekly":
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


def agg_gains(db, periods, rows, interpolate=True, now=None):
    """按快照差值计算各账号每期播放增量; 同时返回最近一期增长Top视频.

    - 基线取该期开始前最后一个快照; 若监控在中途才开始, 以期内首个快照为基线;
    - interpolate=True 时, 期末值由快照序列插值/外推估算(bmon.interp.estimate_at),
      未采集到的当前日/期由此获得展示值; 估算仅用于展示, 不写入数据库;
    - 返回 (gains, latest_top, estimated), estimated 表示本期数字含估算成分.
    """
    from .interp import estimate_at
    now = now or datetime.now()
    bvid_row = {r["bvid"]: r for r in rows}
    if not bvid_row:
        return {}, [], False
    anchor = (periods[0][1] - timedelta(days=10)).strftime(TSFMT)
    end = periods[-1][2].strftime(TSFMT)
    snaps = db.snapshots_between(anchor, end, bvids=set(bvid_row))
    series = {}
    for s in snaps:
        if s["view"] is None:
            continue
        series.setdefault(s["bvid"], []).append(
            (datetime.strptime(s["ts"], TSFMT), s["view"]))  # 已按ts升序

    gains = {}
    latest_top = []
    estimated = False
    last_idx = len(periods) - 1
    for bvid, ser in series.items():
        mid = bvid_row[bvid]["mid"]
        bucket = gains.setdefault(mid, [0] * len(periods))
        times = [t for t, _ in ser]
        for i, (_, s, e) in enumerate(periods):
            t_end = min(e, now)
            if t_end <= s:
                continue
            if interpolate:
                end_v, ext = estimate_at(ser, t_end)
                if end_v is None:
                    continue
            else:
                in_p = [(t, v) for t, v in ser if s <= t < e]
                if not in_p:
                    continue
                end_v, ext = in_p[-1][1], False
            before = [v for t, v in ser if t <= s]
            if before:
                base_v = before[-1]
            else:
                in_p = [(t, v) for t, v in ser if s <= t < t_end]
                if not in_p:
                    continue
                base_v = in_p[0][1]
            g = max(0, int(round(end_v - base_v)))
            bucket[i] += g
            if ext and g > 0:
                estimated = True
            if i == last_idx and g > 0:
                latest_top.append((g, bvid_row[bvid]))
    latest_top.sort(key=lambda x: -x[0])
    return gains, latest_top, estimated


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
    """纸感平设轴样式: 去上/右边框, 墨色底线, 点线网格."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    for side in ("bottom",):
        ax.spines[side].set_color(INK)
        ax.spines[side].set_linewidth(1.1)
    ax.tick_params(colors=DIM_C, labelsize=9.5)
    ax.grid(axis="y", alpha=0.5, linestyle=(0, (2, 4)), linewidth=0.8,
            color=GRID_C)
    ax.set_axisbelow(True)


def _grouped(ax, period_labels, series, colors, labels, title, ylabel):
    n = max(1, len(series))
    m = len(period_labels)
    x = list(range(m))
    width = 0.8 / n
    rotate = m * n > 16
    for i, (mid, vals) in enumerate(series.items()):
        pos = [xi + (i - (n - 1) / 2) * width for xi in x]
        bars = ax.bar(pos, vals, width=width * 0.92, label=labels.get(mid, mid),
                      color=colors.get(mid, "#999"), edgecolor=PAPER,
                      linewidth=0.8, zorder=3)
        ax.bar_label(bars, labels=[fmt_num(v) if v else "" for v in vals],
                     fontsize=8, rotation=90 if rotate else 0, padding=2,
                     color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels(period_labels, rotation=45, ha="right", fontsize=9,
                       color=DIM_C)
    _card_title(ax, title)
    ax.set_ylabel(ylabel, color=DIM_C)
    ax.legend(fontsize=9, frameon=False, loc="upper right",
              handlelength=1.1, handleheight=1.1, borderaxespad=0.2)
    _style_axes(ax)
    if rotate:
        ax.margins(y=0.15)


def _card_title(ax, title, accent=CORAL):
    """编辑部式子图标题: 实色角块 + 墨色粗标题."""
    ax.plot([0], [1.045], "s", color=accent, ms=6,
            transform=ax.transAxes, clip_on=False)
    ax.set_title(title, fontsize=12.5, loc="left", pad=13, color=INK,
                 fontweight="heavy")


def _top(ax, rows, n, colors, labels, title, key=None, note="暂无数据"):
    """横向Top榜(纸感平设): 标题做条目标签(完整折行), 名次mono,
    平涂色条+墨色数值, 游戏以颜色+图例区分."""
    key = key or (lambda r: r.get("latest_view"))
    items = [r for r in rows if key(r) is not None]
    items.sort(key=key, reverse=True)
    items = items[:n]
    if not items:
        ax.text(0.5, 0.5, note, ha="center", va="center",
                transform=ax.transAxes, color=DIM_C, fontsize=12)
        _card_title(ax, title)
        ax.set_xticks([])
        ax.set_yticks([])
        return
    items.reverse()                      # 最大值排最上
    names = [_wrap_title(r.get("title", "")) for r in items]
    max_lines = max(s.count(chr(10)) + 1 for s in names)
    fsize = 9 if max_lines <= 2 else (8.4 if max_lines == 3 else 7.8)
    vals = [key(r) for r in items]
    vmax = max(vals) or 1
    present = []
    for r in items:
        if r.get("mid") not in present:
            present.append(r.get("mid"))
    ys = list(range(len(items)))
    ax.barh(ys, vals, height=0.7,
            color=[colors.get(r.get("mid"), "#999") for r in items],
            edgecolor=PAPER, linewidth=0.8, zorder=3)
    ax.set_yticks(ys)
    ax.set_yticklabels(names, fontsize=fsize, color=INK, linespacing=1.25)
    for i, (y, v) in enumerate(zip(ys, vals)):
        rank = len(items) - i
        ax.text(-vmax * 0.013, y, f"{rank:02d}", ha="right", va="center",
                fontsize=8, color=DIM_C, family="DejaVu Sans Mono")
        if v >= vmax * 0.5:
            ax.text(v - vmax * 0.013, y, fmt_num(v), ha="right", va="center",
                    color="#ffffff", fontsize=8.6, fontweight="bold", zorder=4)
        else:
            ax.text(v + vmax * 0.016, y, fmt_num(v), ha="left", va="center",
                    color=INK, fontsize=8.8,
                    fontweight="bold" if rank <= 3 else "normal", zorder=4)
    ax.set_xlim(-vmax * 0.05, vmax * 1.13)
    _card_title(ax, title, accent=BUTTER)
    ax.set_xticks([])
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=DIM_C, length=0)
    ax.grid(axis="x", alpha=0.5, linestyle=(0, (2, 4)), linewidth=0.8,
            color=GRID_C)
    ax.set_axisbelow(True)
    ax.legend(handles=[Patch(color=colors.get(m, "#999"), label=labels.get(m, m))
                       for m in present],
              loc="lower right", ncols=len(present), fontsize=9, frameon=False)


PERIOD_TITLES = {"daily": "每日", "weekly": "每周", "monthly": "每月"}


def _save(fig, cfg, kind, filename):
    """按周期写入子目录 daily/ weekly/ monthly/, 便于归档与展示分区."""
    outdir = os.path.join(cfg["charts"]["output_dir"], kind)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, filename)
    fig.savefig(path)
    plt.close(fig)
    return path


def scan_chart_groups(cfg):
    """扫描图表目录并按周期分组(每组按时间倒序).

    同时把旧版平铺在根目录的 *_daily_* / *_weekly_* / *_monthly_* 图
    自动迁移进对应子目录, 老数据无缝纳入分区.
    """
    outdir = cfg["charts"]["output_dir"]
    groups = {k: [] for k in ("daily", "weekly", "monthly")}
    if not os.path.isdir(outdir):
        return groups
    for fn in os.listdir(outdir):
        if not fn.endswith(".png"):
            continue
        m = re.search(r"_(daily|weekly|monthly)_", fn)
        if not m:
            continue
        kind = m.group(1)
        sub = os.path.join(outdir, kind)
        os.makedirs(sub, exist_ok=True)
        try:
            os.replace(os.path.join(outdir, fn), os.path.join(sub, fn))
        except OSError:
            pass
    for kind in groups:
        sub = os.path.join(outdir, kind)
        if os.path.isdir(sub):
            files = [f for f in os.listdir(sub) if f.endswith(".png")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(sub, f)),
                       reverse=True)
            groups[kind] = files
    return groups


# ---------- 对外入口 ----------
def make_dashboard(db, cfg, kind, rows):
    cc = cfg["charts"]
    n_back = int(cc.get("periods_back", 12))
    top_n = int(cc.get("top_n", 15))
    periods = build_periods(kind, n_back)
    period_labels = [p[0] for p in periods]
    ordered, labels, colors = account_style(cfg, rows)

    counts = agg_published(rows, periods)
    gains, latest_top, est = agg_gains(db, periods, rows,
                                       interpolate=cc.get("interpolate", True))

    win_start = periods[0][1]
    win_rows = [r for r in rows if r.get("created_ts")
                and datetime.fromtimestamp(r["created_ts"]) >= win_start]
    unit = {"daily": "日", "weekly": "周", "monthly": "个月"}[kind]

    fig, axes = plt.subplots(2, 2, figsize=(17.5, 13), dpi=140)
    fig.patch.set_facecolor(PAPER)
    est_note = " · 含未采集时段插值估算" if est else ""
    fig.patches.append(Rectangle((0.048, 0.962), 0.014, 0.024,
                                 transform=fig.transFigure, facecolor=CORAL,
                                 edgecolor="none", zorder=5))
    fig.text(0.070, 0.962, f"B站官号视频数据{unit}报", fontsize=19,
             color=INK, fontweight="heavy", va="top")
    fig.text(0.952, 0.964,
             f"{period_labels[0]} ~ {period_labels[-1]}  ·  生成于 "
             f"{datetime.now():%Y-%m-%d %H:%M}{est_note}",
             fontsize=10.5, color=DIM_C, ha="right", va="top")
    fig.patches.append(Rectangle((0.048, 0.943), 0.904, 0.0022,
                                 transform=fig.transFigure, facecolor=GRID_C,
                                 edgecolor="none", zorder=1))

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
    return _save(fig, cfg, kind, f"dashboard_{kind}_{period_labels[-1]}.png")


def make_single(db, cfg, kind, ctype, rows):
    cc = cfg["charts"]
    n_back = int(cc.get("periods_back", 12))
    top_n = int(cc.get("top_n", 15))
    periods = build_periods(kind, n_back)
    period_labels = [p[0] for p in periods]
    ordered, labels, colors = account_style(cfg, rows)
    unit = {"daily": "日", "weekly": "周", "monthly": "个月"}[kind]

    if ctype in ("published", "gained"):
        fig, ax = plt.subplots(figsize=(14.5, 7.5), dpi=140)
    else:
        fig, ax = plt.subplots(figsize=(15, 10), dpi=140)
    fig.patch.set_facecolor(PAPER)
    if ctype == "published":
        counts = agg_published(rows, periods)
        _grouped(ax, period_labels,
                 {m: counts.get(m, [0] * len(periods)) for m in ordered},
                 colors, labels, f"每{unit}新发布视频数", "视频数")
    elif ctype == "gained":
        gains, _, est = agg_gains(db, periods, rows,
                                  interpolate=cc.get("interpolate", True))
        est_note = "（含插值估算）" if est else ""
        _grouped(ax, period_labels,
                 {m: gains.get(m, [0] * len(periods)) for m in ordered},
                 colors, labels,
                 f"每{unit}新增播放量(快照差值){est_note}", "播放增量")
    elif ctype == "top":
        win_start = periods[0][1]
        win_rows = [r for r in rows if r.get("created_ts")
                    and datetime.fromtimestamp(r["created_ts"]) >= win_start]
        _top(ax, win_rows, top_n, colors, labels,
             f"近{n_back}{unit}发布视频 · 累计播放 Top{top_n}")
    else:
        raise ValueError(f"未知图表类型: {ctype}")

    fig.tight_layout()
    return _save(fig, cfg, kind, f"{ctype}_{kind}_{period_labels[-1]}.png")


def write_index(cfg):
    groups = scan_chart_groups(cfg)
    outdir = cfg["charts"]["output_dir"]
    os.makedirs(outdir, exist_ok=True)
    ref = int(cfg["charts"].get("auto_refresh_seconds") or 0)
    meta = f'<meta http-equiv="refresh" content="{ref}">' if ref > 0 else ""
    total = sum(len(v) for v in groups.values())
    sections, order = [], ["daily", "weekly", "monthly"]
    for kind in order:
        files = groups[kind][:20]
        if not files:
            continue
        figs = "".join(
            f'<figure><img src="{kind}/{html.escape(fn)}" loading="lazy">'
            f'<figcaption>{html.escape(fn[:-4].replace("_", " · "))}</figcaption></figure>'
            for fn in files)
        sections.append(
            f'<h2 class="sec">{PERIOD_TITLES[kind]}图表'
            f'<span class="cnt">{len(groups[kind])} 张</span></h2>' + figs)
    doc = (
        "<!doctype html><html><head><meta charset=\"utf-8\">" + meta +
        "<title>B站官号视频数据监测</title><style>"
        "body{font-family:'Microsoft YaHei',sans-serif;background:#f7f6f0;color:#1a1c22;"
        "margin:24px;max-width:1500px}"
        "h1{font-size:20px;font-weight:800}h1::before{content:'';display:inline-block;"
        "width:12px;height:12px;background:#ff5a5a;margin-right:10px}"
        "p{color:#7c818e;font-family:Consolas,monospace;font-size:12px}"
        "h2.sec{font-size:16px;margin:36px 0 14px;border-left:4px solid #1a1c22;"
        "padding-left:10px;font-weight:800}"
        "h2 .cnt{font-size:12px;color:#7c818e;margin-left:10px;font-weight:400;"
        "font-family:Consolas,monospace}"
        "figure{margin:0 0 32px}img{max-width:100%;border-radius:8px;background:#fff;"
        "border:1px solid #e4e2d8}"
        "figcaption{color:#7c818e;font-size:13px;margin-top:6px;"
        "font-family:Consolas,monospace}"
        "</style></head><body>"
        "<h1>B站官号视频数据监测 · 自动图表</h1>"
        f"<p>// 共 {total} 张 · 按 日/周/月 分区 · 由监测系统自动更新"
        + (" · 页面每 " + str(ref) + " 秒自动刷新" if ref > 0 else "")
        + "</p>" + "".join(sections) + "</body></html>")
    path = os.path.join(outdir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
