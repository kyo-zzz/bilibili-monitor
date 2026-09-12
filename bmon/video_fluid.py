"""数据变化可视化视频 · "Fluid Geometry" 流体几何风渲染器.

与经典版(bmon/video.py)共享数据聚合与时间轴算法, 仅视觉层重设计:
平面设计排版 × 静止系MAD —— 超大描边底字 / mono注释 / 色块与留白 /
液态漂移色斑 / 环形文字 / 十字准星 / 辉光曲线 / 圆角渐变条形.
输出 1920x1080 H.264 MP4; 经典版经 `--style classic` 仍可随时使用.
"""
import logging
import math
import os
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.animation import FFMpegWriter, FuncAnimation
from matplotlib.patches import FancyBboxPatch, Polygon, Rectangle
from matplotlib.colors import LinearSegmentedColormap

import numpy as np

from .util import fmt_num
from .video import (TSFMT, _bar_axis, _interp_value,
                    _side_timeline, _time_axis, _wrap2, collect, setup_font)

log = logging.getLogger("bmon.video_fluid")

# ---------- 调色板(与 Web UI "Paper Editorial" 同源: 浅色纸感+平涂流行色) ----------
BG1, BG2 = "#f7f6f0", "#eeece4"
PANEL = "#ffffff"
EDGE = "#e2e0d4"
INK = "#1a1c22"
DIM = "#7c818e"
CYAN, PINK, VIOLET = "#ff5a5a", "#f5b93f", "#8f7bff"   # 珊瑚/奶油/雾紫(平涂)
GOLD, GREEN = "#e8a13a", "#2fbf71"
GRID = "#e8e6db"


# ---------- 基础元件 ----------
def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _fade(i, n, fin=15, fout=10):
    return min(1.0, i / fin) * min(1.0, (n - i) / fout)


def _full_ax(fig):
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return ax


def _bg(ax, t):
    """纸感渐变底 + 漂移柔色斑 + 网格点."""
    grad = np.linspace(0, 1, 256).reshape(-1, 1)
    cmap = LinearSegmentedColormap.from_list("bg", [BG1, BG2])
    ax.imshow(grad, extent=(0, 1, 0, 1), origin="lower", aspect="auto",
              cmap=cmap, zorder=0, interpolation="bicubic")
    for (cx, cy, r, col, al, ph) in (
            (0.16, 0.88, 0.30, PINK, 0.16, 0.0),
            (0.93, 0.16, 0.34, VIOLET, 0.12, 2.1),
            (0.30, 0.05, 0.22, "#9fe3bd", 0.14, 4.2)):
        dx = 0.025 * math.sin(t * 0.45 + ph)
        dy = 0.020 * math.cos(t * 0.36 + ph * 1.3)
        _blob(ax, cx + dx, cy + dy, r, col, al, t, ph, layers=3, z=1)
    # 网格点
    xs = np.arange(0.06, 1.0, 0.055)
    ys = np.arange(0.06, 1.0, 0.055)
    gx, gy = np.meshgrid(xs, ys)
    ax.scatter(gx, gy, s=1.1, color=INK, alpha=0.06, zorder=1)


def _blob(ax, cx, cy, r, color, alpha, t, seed=0.0, layers=3, z=2):
    """液态变形圆: 多层同心柔光 + 正弦谐波扰动轮廓."""
    for L in range(layers, 0, -1):
        rr = r * (1 + 0.32 * (L - 1))
        al = alpha * (0.30 ** (L - 1))
        pts = []
        for k in range(56):
            ang = 2 * math.pi * k / 56
            wob = (0.10 * math.sin(3 * ang + t * 0.9 + seed)
                   + 0.07 * math.sin(5 * ang - t * 0.7 + seed * 2))
            rad = rr * (1 + wob)
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        ax.add_patch(Polygon(pts, closed=True, facecolor=color,
                             edgecolor="none", alpha=al, zorder=z))


def _outline(ax, x, y, s, size, alpha, ha="left", weight="heavy", zorder=1):
    """描边空心大字(静止系底字)."""
    txt = ax.text(x, y, s, fontsize=size, fontweight=weight, color="none",
                  ha=ha, va="top", zorder=zorder, family="DejaVu Sans")
    txt.set_path_effects([pe.Stroke(linewidth=1.5, foreground=INK)])
    txt.set_alpha(max(0.0, min(1.0, alpha)) * 0.20)
    return txt


def _cross(ax, x, y, s=0.012, alpha=0.4, lw=1.2, zorder=2):
    ax.plot([x - s, x + s], [y, y], color=INK, lw=lw, alpha=alpha * 0.55, zorder=zorder)
    ax.plot([x, x], [y - s, y + s], color=INK, lw=lw, alpha=alpha * 0.55, zorder=zorder)


def _sparkles(ax, pts, t, alpha):
    for (x, y, ms, ph) in pts:
        tw = 0.5 + 0.5 * math.sin(t * 2.2 + ph)
        ax.plot([x], [y], "*", ms=ms, color=GOLD,
                alpha=alpha * (0.30 + 0.55 * tw), zorder=5,
                markeredgecolor="none")


def _panel(ax, x, y, w, h, alpha=1.0, z=2, ec=EDGE, rounding=0.010):
    box = FancyBboxPatch((x, y), w, h,
                         boxstyle=f"round,pad=0,rounding_size={rounding}",
                         facecolor=PANEL, edgecolor=ec, linewidth=1.2,
                         alpha=alpha, transform=ax.transAxes,
                         mutation_aspect=16 / 9, zorder=z)
    ax.add_patch(box)
    return box


def _chip(ax, x, y, w, h, c1, c2=None, alpha=1.0, z=4, rounding=0.016):
    """平涂圆角色块(流行色块面)."""
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle=f"round,pad=0,rounding_size={rounding}",
                                facecolor=c1, edgecolor="none",
                                alpha=alpha, transform=ax.transAxes,
                                mutation_aspect=16 / 9, zorder=z))


def _ring_text(ax, cx, cy, r, s, size, alpha, color=DIM, t=0.0, z=2):
    """环形排布文字(MAD素材味), 随 t 缓慢公转."""
    n = len(s)
    a0 = -90 + (t * 4) % (360 / n)
    circle = plt.Circle((cx, cy), r, fill=False, edgecolor=INK,
                        linewidth=1.0, alpha=alpha * 0.16, zorder=z)
    ax.add_patch(circle)
    circle2 = plt.Circle((cx, cy), r * 0.86, fill=False, edgecolor=INK,
                         linewidth=0.8, alpha=alpha * 0.10, zorder=z)
    ax.add_patch(circle2)
    for k, ch in enumerate(s):
        ang = a0 + 360.0 * k / n
        rad = math.radians(ang)
        ax.text(cx + r * math.cos(rad), cy + r * math.sin(rad), ch,
                fontsize=size, color=color, alpha=alpha * 0.55,
                rotation=ang + 90, ha="center", va="center", zorder=z)


def _header(ax, i, n, en, title, sub, period, accent=CYAN):
    """统一页头: 描边底字 + 实色角块 + 标题 + mono副题 + 右侧时期."""
    a = _fade(i, n)
    t = i / 24
    _outline(ax, 0.062, 0.985, en, 64, a)
    e = _ease(min(1.0, i / 12))
    # 实色几何角块: 一实一空, 错位叠放
    ax.add_patch(Rectangle((0.062, 0.908), 0.030 * e, 0.026,
                           facecolor=accent, edgecolor="none", alpha=a,
                           transform=ax.transAxes, zorder=3))
    ax.add_patch(Rectangle((0.062 + 0.036 * e, 0.916), 0.018 * e, 0.018,
                           facecolor="none", edgecolor=PINK, linewidth=1.6,
                           alpha=a, transform=ax.transAxes, zorder=3))
    ax.text(0.105, 0.930, title, fontsize=21.5, color=INK, fontweight="bold",
            alpha=a, zorder=4)
    if sub:
        ax.text(0.105, 0.893, "// " + sub, fontsize=11, color=DIM,
                alpha=a, zorder=4)
    if period:
        ax.text(0.938, 0.930, period, fontsize=11, color=DIM, ha="right",
                family="DejaVu Sans Mono", alpha=a, zorder=4)
    _cross(ax, 0.955, 0.885, 0.008, a, zorder=4)
    return a, t


def _num_roll(ax, x, y, val, p, size, color, a, ha="center", prefix=""):
    ax.text(x, y, prefix + fmt_num(int(val * p)), fontsize=size, color=color,
            ha=ha, va="center", fontweight="bold", alpha=a, zorder=5)


# ---------- 场景 ----------
def _o(data, scene, key, dflt=None):
    """读取场景级展示参数: data["_o"]["scenes"][scene][key]."""
    return (((data.get("_o") or {}).get("scenes") or {}).get(scene) or {}).get(
        key, dflt)


def _sc_title(fig, i, n, data):
    ax = _full_ax(fig)
    t = i / max(1, n - 1)
    a = _fade(i, n)
    _bg(ax, i / 10)
    p = _ease(t)
    _outline(ax, 0.5, 0.78, "DATA REPORT", 88, a, ha="center")
    ax.text(0.5, 0.585, data.get("_title", "B站官号数据变化报告"),
            fontsize=float(_o(data, "title", "title_size", 47)), color=INK,
            ha="center", fontweight="bold", alpha=a, zorder=4)
    # 平涂胶囊时期牌
    pw = 0.30 * p
    _chip(ax, 0.5 - pw / 2, 0.455, pw, 0.052, CYAN, alpha=a, z=4)
    ax.text(0.5, 0.481, f"{data['ts_from'][:10]}  →  {data['ts_to'][:10]}",
            fontsize=15.5, color="#ffffff", ha="center", va="center",
            fontweight="bold", alpha=a * p, zorder=5)
    ax.text(0.5, 0.395, data.get("_subtitle", "VIEW / SNAPSHOT / LOCAL ARCHIVE"),
            fontsize=11.5, color=DIM, ha="center", family="DejaVu Sans Mono",
            alpha=a * p, zorder=4)
    if _o(data, "title", "blocks", True):
        ax.add_patch(Rectangle((0.075, 0.135), 0.052, 0.052, facecolor=PINK,
                               edgecolor="none", alpha=a * 0.9, zorder=3))
        ax.add_patch(Rectangle((0.105, 0.165), 0.052, 0.052, facecolor="none",
                               edgecolor=INK, linewidth=1.8, alpha=a, zorder=3))
        ax.add_patch(Rectangle((0.135, 0.135), 0.022, 0.022, facecolor=VIOLET,
                               edgecolor="none", alpha=a * 0.85, zorder=3))
    if _o(data, "title", "ring", True):
        _ring_text(ax, 0.845, 0.70, 0.115,
                   "BILIBILI MONITOR ★ FLUID GEOMETRY ★ ", 10.5, a, t=i / 8)
    _sparkles(ax, [(0.22, 0.66, 13, 0.5), (0.72, 0.30, 11, 2.2),
                   (0.35, 0.24, 8, 4.0), (0.63, 0.72, 9, 5.1)], i / 10, a)
    ax.text(0.5, 0.075, "bmon · bilibili-monitor", fontsize=10.5, color=DIM,
            ha="center", family="DejaVu Sans Mono", alpha=a * 0.7, zorder=4)


def _sc_overview(fig, i, n, data):
    ax = _full_ax(fig)
    a, t = _header(ax, i, n, "OVERVIEW", "总览 · 数据卡片",
                   "全账号合计与分游戏明细", _period(data), accent=CYAN)
    s = data["summary"]
    p = _ease(i / max(1, n - 22))
    cards = [
        ("VIDEOS", "监测视频", s["videos"], INK, CYAN),
        ("PLAYS", "累计播放", s["views"], INK, PINK),
        ("DELTA", "本期播放增量", s["growth"], GREEN, VIOLET),
    ]
    for k, (en, label, val, col, ac) in enumerate(cards):
        x = 0.07 + k * 0.30
        _panel(ax, x, 0.60, 0.27, 0.215, alpha=a)
        ax.add_patch(Rectangle((x + 0.018, 0.775), 0.10, 0.008, facecolor=ac,
                               edgecolor="none", alpha=a, zorder=4))
        ax.text(x + 0.018, 0.855, en, fontsize=10, color=DIM,
                family="DejaVu Sans Mono", alpha=a, zorder=4)
        ax.text(x + 0.252, 0.782, f"0{k+1}", fontsize=12, color=DIM,
                ha="right", family="DejaVu Sans Mono", alpha=a * 0.7, zorder=4)
        ax.text(x + 0.018, 0.715, label, fontsize=12.5, color=DIM, alpha=a,
                zorder=4)
        _num_roll(ax, x + 0.018, 0.648, val, p, 27, col, a, ha="left",
                  prefix="+" if en == "DELTA" else "")
    _cross(ax, 0.955, 0.66, 0.008, a)

    # 分游戏行: 色点 + 名称 + 播放占比条 + 指标
    accs = data["accounts"][:max(1, int(_o(data, "overview", "game_rows", 3)))]
    total_views = max(1, sum(x["views"] for x in accs))
    ax.text(0.07, 0.545, "分游戏明细 / BY GAME", fontsize=11, color=DIM,
            alpha=a, zorder=4)
    for k, acc in enumerate(accs):
        y = 0.40 - k * 0.115
        _panel(ax, 0.07, y, 0.86, 0.092, alpha=a)
        ax.plot([0.096], [y + 0.046], "o", color=acc["color"], ms=10, alpha=a,
                zorder=4)
        ax.plot([0.096], [y + 0.046], "o", color=acc["color"], ms=18,
                alpha=a * 0.18, zorder=4)
        ax.text(0.120, y + 0.046, acc["name"], fontsize=15, color=INK,
                va="center", fontweight="bold", alpha=a, zorder=4)
        # 占比条
        bw = 0.20 * (acc["views"] / total_views) * p
        ax.add_patch(FancyBboxPatch((0.28, y + 0.020), max(0.004, bw), 0.014,
                                    boxstyle="round,pad=0,rounding_size=0.007",
                                    facecolor=acc["color"], edgecolor="none",
                                    alpha=a * 0.95, transform=ax.transAxes,
                                    mutation_aspect=16 / 9, zorder=4))
        ax.text(0.28, y + 0.062, "播放占比", fontsize=8.5, color=DIM, alpha=a,
                zorder=4)
        cols = [("视频数", fmt_num(int(acc["videos"] * p)), INK),
                ("累计播放", fmt_num(int(acc["views"] * p)), INK),
                ("本期增量", "+" + fmt_num(int(acc["growth"] * p)), GREEN)]
        for c, (cl, cv, cc) in enumerate(cols):
            cx = 0.545 + c * 0.135
            ax.text(cx, y + 0.064, cl, fontsize=9.5, color=DIM, ha="center",
                    alpha=a, zorder=4)
            ax.text(cx, y + 0.028, cv, fontsize=15.5, color=cc, ha="center",
                    fontweight="bold", alpha=a, zorder=4)
        _outline(ax, 0.905, y + 0.085, f"0{k+1}", 22, a, ha="right")
    # 底部时间轴: 两端=起末时间, 内部逐日刻度避让(标签向内对齐不越出轴线)
    t0, t1 = _time_axis(data)
    span = (t1 - t0).total_seconds()
    ty = 0.095
    ticks, labeled = _day_cells(t0, t1)
    ax.plot([0.07, 0.93], [ty, ty], color=EDGE, lw=1.4, alpha=a, zorder=3)
    for day in ticks:
        gx = 0.07 + 0.86 * (day - t0).total_seconds() / span
        ax.plot([gx, gx], [ty, ty + 0.009], color=EDGE, lw=1.1, alpha=a, zorder=3)
    for day in labeled:
        gx = 0.07 + 0.86 * (day - t0).total_seconds() / span
        ax.text(gx, ty - 0.028, day.strftime("%m-%d"), fontsize=9,
                color=DIM, ha=_edge_ha(gx, 0.07, 0.93), alpha=a, zorder=3)
    px = 0.07 + 0.86 * p
    ax.plot([0.07, px], [ty, ty], color=CYAN, lw=2.4, alpha=a, zorder=4)
    ax.plot([px], [ty], "o", color=CYAN, ms=6, alpha=a, zorder=5)
    ax.plot([px], [ty], "o", color=CYAN, ms=14, alpha=a * 0.2, zorder=4)
    cur = min(t0 + (t1 - t0) * p, t1 - timedelta(seconds=1))
    ax.text(px, ty + 0.022, cur.strftime("%m-%d"),
            fontsize=9.5, color=CYAN, ha="center",
            fontweight="bold", alpha=a, zorder=5)


def _period(data):
    return f"{data['ts_from'][:10]} ~ {data['ts_to'][:10]}"


def _time_axis(data):
    """轴域: 起点=起始日 00:00(保证内部日期格均匀), 末端=末时间本身
    (右边缘就是最后一个数据时刻, 其后不再留空)."""
    t0 = datetime.strptime(data["ts_from"], TSFMT)
    t1 = datetime.strptime(data["ts_to"], TSFMT)
    if t1 <= t0:
        t1 = t0 + timedelta(days=1)
    d0 = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    return d0, t1


def _day_cells(t0d, t1, max_labels=15):
    """返回 (全部刻度, 需标注刻度). 轴域 [起始日00:00, 末时间]:

    刻度=起点 + 每个自然日0点 + 末端(=末时间); 末时间所在日的 0 点格线删去
    (09-10 直连末端一格, 不多出半格); 标注=起点日期 + 各日0点日期, 与末时间
    同日的 0 点不标注(该日期由末端承担, 保证最右侧就是末时间且其后无空白).
    长跨度按 k 格均匀抽稀.
    """
    ticks = [t0d]
    m = t0d + timedelta(days=1)
    while m < t1:
        if m.date() != t1.date():                 # 末日的0点格线删去
            ticks.append(m)
        m += timedelta(days=1)
    ticks.append(t1)                              # 末端=末时间
    n_cells = max(1, int((t1 - t0d).total_seconds() // 86400))
    k = max(1, -(-n_cells // max(4, max_labels)))  # ceil
    mids = ticks[1:-1]
    labeled = [t0d] + [t for i, t in enumerate(mids, 1) if i % k == 0] + [t1]
    return ticks, labeled


def _edge_ha(gx, x_lo, x_hi, margin=0.035):
    """端点附近标签向内对齐, 防止越出轴线两端."""
    if gx - x_lo < margin:
        return "left"
    if x_hi - gx < margin:
        return "right"
    return "center"


def _axis_days(ax, t0, t1, x0, x1, y0, a, y1):
    span = (t1 - t0).total_seconds()
    ticks, labeled = _day_cells(t0, t1)
    ax.plot([x0, x1], [y0, y0], color=EDGE, lw=1.3, alpha=a, zorder=3)
    for day in ticks:
        gx = x0 + (x1 - x0) * (day - t0).total_seconds() / span
        ax.plot([gx, gx], [y0, y1], color=GRID, lw=0.9, alpha=a * 0.9, zorder=2)
    for day in labeled:
        gx = x0 + (x1 - x0) * (day - t0).total_seconds() / span
        ax.text(gx, y0 - 0.030, day.strftime("%m-%d"), fontsize=10,
                color=DIM, ha=_edge_ha(gx, x0, x1), alpha=a, zorder=3)


def _glow_curve(ax, xs, ys, color, lw, a, z=4):
    ax.plot(xs, ys, color=color, lw=lw * 2.6, alpha=a * 0.16,
            solid_capstyle="round", zorder=z)
    ax.plot(xs, ys, color=color, lw=lw, alpha=a * 0.98,
            solid_capstyle="round", zorder=z + 1)


def _trend_like(fig, i, n, data, kind, trend_items=None, scene=None,
                en=None, title=None, sub=None, accent=None):
    """播放量走势 / 净增量走势·视频 / 净增量·近期视频 共用骨架."""
    ax = _full_ax(fig)
    a = _fade(i, n)
    _bg(ax, i / 10)
    sc = scene or ("trend" if kind == "view" else "gains_videos")
    top_n = max(1, int(_o(data, sc, "top", 8)))
    pool = trend_items if trend_items is not None else data["trend"]
    trend = pool[:top_n]
    line_w = float(_o(data, sc, "line_width", 2.6))
    show_dots = bool(_o(data, sc, "dots", True))
    label_w = int(_o(data, sc, "label_width", 24))
    axis_pad = float(_o(data, sc, "axis_pad", 0.12))
    if en is None:
        en = "TRENDING" if kind == "view" else "GAINS / VIDEO"
    if title is None:
        title = "播放量走势" if kind == "view" else "净增量走势 · 视频"
    if sub is None:
        sub = (f"变化最显著的 Top{len(trend)} 视频 · 辉光曲线"
               if kind == "view" else f"Top{len(trend)} 视频各自净增量 · 零基线")
    if accent is None:
        accent = CYAN if kind == "view" else VIOLET
    _header(ax, i, n, en, title, sub, _period(data), accent=accent)
    if not trend:
        ax.text(0.5, 0.45, "本期暂无趋势数据", fontsize=19, color=DIM,
                ha="center", alpha=a)
        return
    t0, t1 = _time_axis(data)
    x0, x1, y0, y1 = 0.09, 0.70, 0.13, 0.80
    _panel(ax, 0.065, 0.11, 0.655, 0.72, alpha=a)
    if kind == "view":
        vmin = min(v for it in trend for _, v in it["pts"])
        vmax = max(v for it in trend for _, v in it["pts"])
        lo = max(0, vmin - (vmax - vmin) * axis_pad)
        hi = vmax + (vmax - vmin) * 0.08 or 1
    else:
        gmin = min(min(v for _, v in it["pts"]) - it["start"] for it in trend)
        gmax = max(it["growth"] for it in trend) * 1.08 or 1
        if _o(data, sc, "zero_base", True):
            lo = 0
        else:
            lo = max(0, gmin - (gmax - gmin) * axis_pad)
        hi = gmax

    def tx(tt):
        return x0 + (x1 - x0) * (tt - t0).total_seconds() / (t1 - t0).total_seconds()

    def vy(v):
        v = max(0.0, v) if kind == "gain" else v
        return y0 + (y1 - y0) * (v - lo) / (hi - lo)

    _axis_days(ax, t0, t1, x0, x1, y0, a, y1)
    for frac in ((0.0, 0.33, 0.66, 1.0) if kind == "view" else (0.33, 0.66, 1.0)):
        v = lo + (hi - lo) * frac
        ax.text(x0 - 0.012, vy(v), fmt_num(int(v)), fontsize=9.5, color=DIM,
                ha="right", va="center", alpha=a, zorder=3)
        if frac > 0:
            ax.plot([x0, x1], [vy(v)] * 2, color=GRID, lw=0.9,
                    linestyle=(0, (4, 4)), alpha=a, zorder=2)

    pad = data.get("_pads", {}).get(sc, max(6, int(n * 0.25)))
    sweep = t0 + (t1 - t0) * min(1.0, i / max(1, n - pad))
    for it in trend:
        f0 = it["pts"][0][0]
        xs, ys = [], []
        for k in range(121):
            tt = t0 + (sweep - t0) * k / 120     # 折线一律从轴起点(09-04)开始
            if tt < f0:
                # 首个快照之前: 增长幕贴轴为零; 播放量幕平推首值
                v = it["pts"][0][1] if kind == "view" else it["start"]
            else:
                v = _interp_value(it["pts"], tt)
            if kind == "gain":
                v -= it["start"]
            xs.append(tx(tt))
            ys.append(vy(v))
        _glow_curve(ax, xs, ys, it["color"], line_w, a)
        if show_dots:
            mp = [(tt, v) for tt, v in it["pts"] if tt <= sweep]
            ax.plot([tx(tt) for tt, _ in mp],
                    [vy(v - it["start"] if kind == "gain" else v) for _, v in mp],
                    "o", ms=5.5, color=it["color"], markerfacecolor=it["color"],
                    markeredgecolor=BG1, markeredgewidth=1.0, alpha=a, zorder=6)
    if sweep < t1:
        gx = tx(sweep)
        ax.plot([gx, gx], [y0, y1], color=DIM, lw=1, linestyle=(0, (3, 4)),
                alpha=a * 0.5, zorder=3)

    key = f"_side_{sc}"
    pad = data.get("_pads", {}).get(
        sc, max(6, int(n * 0.25)))
    side = data.get(key)
    if side is None:
        fn = (lambda it, tt: _interp_value(it["pts"], tt)) if kind == "view" \
            else (lambda it, tt: _interp_value(it["pts"], tt) - it["start"])
        side = data[key] = _side_timeline(trend, t0, t1, n, lo, hi, y0, y1, fn,
                                          side_pad=pad)
    fr = side[min(i, len(side) - 1)]
    for it in trend:
        sy = fr["ys"][it["bvid"]]
        ax.plot([0.735], [sy], "o", color=it["color"], ms=7.5, alpha=a,
                zorder=5)
        ax.plot([0.735], [sy], "o", color=it["color"], ms=16, alpha=a * 0.2,
                zorder=4)
        ax.text(0.756, sy, _wrap2(it["title"], label_w), fontsize=9.5,
                color=INK, va="center", linespacing=1.18, alpha=a, zorder=5)
        val = fr["vals"][it["bvid"]]
        txt = (fmt_num(int(val)) if kind == "view"
               else "+" + fmt_num(max(0, int(val))))
        ax.text(0.985, sy, txt, fontsize=11.5,
                color=it["color"] if kind == "view" else GREEN,
                va="center", ha="right", fontweight="bold", alpha=a, zorder=5)
    _sparkles(ax, [(0.93, 0.20, 10, 1.0), (0.90, 0.62, 8, 3.3)], i / 10, a)


def _sc_trend(fig, i, n, data):
    _trend_like(fig, i, n, data, "view")


def _sc_gains_videos(fig, i, n, data):
    _trend_like(fig, i, n, data, "gain")


def _recent_filter(items, t0, t1, top_n=8, mult=2.0):
    """仅收录发布时间在 数据跨度×mult 内的视频; 若不足 top_n 个,
    按发布时间从近到远继续向前回溯补满(兜底).
    返回 (按期末播放降序列表, 实际最早发布时间戳, 是否触发兜底)."""
    cutoff = (t1 - (t1 - t0) * mult).timestamp()
    in_range = [it for it in items if (it.get("created_ts") or 0) >= cutoff]
    if len(in_range) >= top_n:
        return sorted(in_range, key=lambda x: -x["end"]), cutoff, False
    # 兜底: 目标范围不足, 从最新往旧补满
    picked = {it["bvid"] for it in in_range}
    rest = sorted((it for it in items if it["bvid"] not in picked),
                  key=lambda x: -(x.get("created_ts") or 0))
    out = list(in_range) + rest[:max(0, top_n - len(in_range))]
    eff = min((it.get("created_ts") or 0) for it in out) if out else cutoff
    return sorted(out, key=lambda x: -x["end"]), eff, True


def _sc_gains_recent(fig, i, n, data):
    rt0 = datetime.strptime(data["ts_from"], TSFMT)
    rt1 = datetime.strptime(data["ts_to"], TSFMT)
    top_n = max(1, int(_o(data, "gains_recent", "top", 8)))
    span_days = max(1, round((rt1 - rt0).total_seconds() / 86400))
    pool, eff_ts, fell_back = _recent_filter(
        data.get("trend_all") or [], rt0, rt1, top_n=top_n, mult=2.0)
    cutoff_d = (rt1 - (rt1 - rt0) * 2).strftime("%m-%d")
    sub = f"仅收录 {cutoff_d} 后发布的视频(数据跨度 {span_days} 天 × 2)"
    if fell_back:
        sub += f" · 不足 {top_n} 个已兜底回溯至 " \
               f"{datetime.fromtimestamp(eff_ts).strftime('%m-%d')}"
    sub += f" · 命中 {len(pool)} 个 · 零基线"
    _trend_like(
        fig, i, n, data, "gain", trend_items=pool, scene="gains_recent",
        en="GAINS / RECENT", title="净增量走势 · 近期视频",
        sub=sub, accent=GREEN)


def _sc_gains_games(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _bg(ax, i / 10)
    _header(ax, i, n, "GAINS / GAMES", "净增量走势 · 分游戏",
            "三个游戏全部监测视频的合计净增量", _period(data), accent=PINK)
    acc_gains = data["acc_gains"]
    if not acc_gains:
        ax.text(0.5, 0.45, "本期暂无增量数据", fontsize=19, color=DIM,
                ha="center", alpha=a)
        return
    t0, t1 = _time_axis(data)
    x0, x1, y0, y1 = 0.09, 0.70, 0.13, 0.80
    _panel(ax, 0.065, 0.11, 0.655, 0.72, alpha=a)
    gmax = max(g["end"] for g in acc_gains) * 1.10 or 1

    def tx(tt):
        return x0 + (x1 - x0) * (tt - t0).total_seconds() / (t1 - t0).total_seconds()

    def vy(v):
        return y0 + (y1 - y0) * max(0.0, v) / gmax

    _axis_days(ax, t0, t1, x0, x1, y0, a, y1)
    for frac in (0.33, 0.66, 1.0):
        ax.text(x0 - 0.012, vy(gmax * frac), fmt_num(int(gmax * frac)),
                fontsize=9.5, color=DIM, ha="right", va="center",
                alpha=a, zorder=3)
        ax.plot([x0, x1], [vy(gmax * frac)] * 2, color=GRID, lw=0.9,
                linestyle=(0, (4, 4)), alpha=a, zorder=2)

    pad = data.get("_pads", {}).get("gains_games", max(6, int(n * 0.25)))
    sweep = t0 + (t1 - t0) * min(1.0, i / max(1, n - pad))
    for g in acc_gains:
        pts = [(tt, v) for tt, v in g["pts"] if tt <= sweep]
        if not pts:
            continue
        _glow_curve(ax, [tx(tt) for tt, _ in pts],
                    [vy(v) for _, v in pts], g["color"], 3.2, a)
        ax.plot([tx(pts[-1][0])], [vy(pts[-1][1])], "o", color=g["color"],
                ms=7.5, markeredgecolor=BG1, markeredgewidth=1.0, alpha=a,
                zorder=6)
    if sweep < t1:
        gx = tx(sweep)
        ax.plot([gx, gx], [y0, y1], color=DIM, lw=1, linestyle=(0, (3, 4)),
                alpha=a * 0.5, zorder=3)

    p = _ease(i / max(1, n - 22))
    for k, g in enumerate(acc_gains[:3]):
        sy = 0.735 - k * 0.125
        cur = next((v for tt, v in reversed(g["pts"]) if tt <= sweep), 0)
        _panel(ax, 0.725, sy - 0.048, 0.255, 0.096, alpha=a)
        ax.plot([0.748], [sy], "o", color=g["color"], ms=10, alpha=a, zorder=5)
        ax.plot([0.748], [sy], "o", color=g["color"], ms=20, alpha=a * 0.18,
                zorder=4)
        ax.text(0.770, sy, g["name"], fontsize=12.5, color=INK, va="center",
                fontweight="bold", alpha=a, zorder=5)
        _num_roll(ax, 0.962, sy, cur, p, 14.5, GREEN, a, ha="right", prefix="+")
    _sparkles(ax, [(0.90, 0.16, 9, 2.0)], i / 10, a)


def _sc_bars(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _bg(ax, i / 10)
    _header(ax, i, n, "BAR RACE", "播放量竞跑 Top10",
            "条形长度 = 当前播放量 · 条尾为期内新增", _period(data), accent=GOLD)
    race_top = max(1, int(_o(data, "bars", "top", 10)))
    thr = float(_o(data, "bars", "truncate_thr", 0.45))
    bar_h = float(_o(data, "bars", "bar_h", 0.56))
    data["tops"] = data["tops"][:race_top]
    tops = data["tops"]
    if not tops:
        ax.text(0.5, 0.45, "本期暂无增量数据", fontsize=19, color=DIM,
                ha="center", alpha=a)
        return
    race = data.get("_race")
    if race is None:
        pad_b = data.get("_pads", {}).get("bars", max(6, int(n * 0.25)))
        race = data["_race"] = _race_timeline_local(
            data, n, hold=max(4, int(n * 0.04)), hold_end=pad_b)
    fr = race[min(i, len(race) - 1)]
    t0, t1 = _time_axis(data)
    left, right = 0.30, 0.80
    top_y, bot_y = 0.80, 0.185
    step = (top_y - bot_y) / len(tops)
    axis_min, axis_max = _bar_axis(tops, thr=thr)

    def bw(v):
        return (right - left) * max(0.0, v - axis_min) / (axis_max - axis_min)

    for frac in (0.0, 1 / 3, 2 / 3, 1.0):
        val = axis_min + (axis_max - axis_min) * frac
        gx = left + (right - left) * frac
        ax.plot([gx, gx], [bot_y, top_y], color=GRID, lw=0.9, alpha=a, zorder=2)
        ax.text(gx, 0.842, fmt_num(int(val)), fontsize=9, color=DIM,
                ha="center", alpha=a, zorder=3)
    ax.text(0.985, 0.842, "当前播放量", fontsize=9, color=DIM, ha="right",
            alpha=a, zorder=3)

    for rank, (it, v) in enumerate(fr["vals"]):
        y = fr["ys"][it["bvid"]]
        w = max(0.0015, bw(v))
        # 名次 mono
        ax.text(left - 0.058, y, f"{rank+1:02d}", fontsize=10, color=DIM,
                va="center", family="DejaVu Sans Mono", alpha=a * 0.85,
                zorder=3)
        ax.text(left - 0.022, y, _wrap2(it["title"], 30), fontsize=8.8,
                color=INK, ha="right", va="center", linespacing=1.12,
                alpha=a, zorder=3)
        # 条形阴影 + 圆角渐变条
        ax.add_patch(FancyBboxPatch((left + 0.003, y - step * 0.28 + 0.0022),
                                    max(0.001, w), step * bar_h,
                                    boxstyle="round,pad=0,rounding_size=0.006",
                                    facecolor="#000000", edgecolor="none",
                                    alpha=a * 0.28, transform=ax.transAxes,
                                    mutation_aspect=16 / 9, zorder=3))
        _chip(ax, left, y - step * 0.28, w, step * bar_h,
              it["color"], _lighten(it["color"], 0.45), alpha=a * 0.96,
              z=4, rounding=0.005)
        gain = int(max(0, v - it["start"]))
        if gain > 0:
            ax.text(left + w + 0.010, y, "+" + fmt_num(gain), fontsize=10.5,
                    color=GOLD if rank < 3 else INK, va="center",
                    fontweight="bold" if rank < 3 else "normal",
                    alpha=a, zorder=5)
        ax.text(0.985, y, fmt_num(int(v)), fontsize=10.5, color=DIM,
                va="center", ha="right", alpha=a, zorder=5)

    # 底部日期轴 + 进度(两端=起末时间, 标签向内对齐不越出轴线)
    span = (t1 - t0).total_seconds()
    ty = 0.112
    ticks, labeled = _day_cells(t0, t1)
    ax.plot([left, right], [ty, ty], color=EDGE, lw=1.4, alpha=a, zorder=3)
    for day in ticks:
        gx = left + (right - left) * (day - t0).total_seconds() / span
        ax.plot([gx, gx], [ty, ty + 0.008], color=EDGE, lw=1.0, alpha=a,
                zorder=3)
    for day in labeled:
        gx = left + (right - left) * (day - t0).total_seconds() / span
        ax.text(gx, ty - 0.026, day.strftime("%m-%d"), fontsize=8.5,
                color=DIM, ha=_edge_ha(gx, left, right), alpha=a, zorder=3)
    px = left + (right - left) * (fr["t"] - t0).total_seconds() / span
    ax.plot([left, px], [ty, ty], color=CYAN, lw=2.4, alpha=a, zorder=4)
    ax.plot([px], [ty], "o", color=CYAN, ms=6, alpha=a, zorder=5)
    ax.plot([px], [ty], "o", color=CYAN, ms=13, alpha=a * 0.2, zorder=4)
    cur = min(fr["t"], t1 - timedelta(seconds=1))
    ax.text(px, ty + 0.020, cur.strftime("%m-%d"), fontsize=9.5,
            color=CYAN, ha="center",
            fontweight="bold", alpha=a, zorder=5)

    # 图例
    seen, lx = [], 0.07
    for it in tops:
        if it["account"] in seen:
            continue
        seen.append(it["account"])
        ax.plot([lx], [0.048], "s", color=it["color"], ms=7, alpha=a,
                zorder=4)
        ax.text(lx + 0.014, 0.048, it["account"], fontsize=10.5, color=DIM,
                va="center", alpha=a, zorder=4)
        lx += 0.014 + 0.016 * len(it["account"]) + 0.030


def _lighten(hex_color, amount=0.4):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _sc_end(fig, i, n, data):
    ax = _full_ax(fig)
    t = i / 10
    a = _fade(i, n)
    _bg(ax, t)
    p = _ease(i / max(1, n - 1))
    _ring_text(ax, 0.5, 0.47, 0.185, "THANKS FOR WATCHING ★ LOCAL DATA ★ ",
               12, a, t=i / 8)
    _outline(ax, 0.5, 0.76, "SEE YOU", 56, a, ha="center")
    ax.text(0.5, 0.505, _o(data, "end", "text", "本期报告 · 完"), fontsize=36, color=INK, ha="center",
            fontweight="bold", alpha=a, zorder=4)
    pw = 0.22 * p
    _chip(ax, 0.5 - pw / 2, 0.415, pw, 0.042, PINK, VIOLET, alpha=a, z=4)
    ax.text(0.5, 0.436, _period(data), fontsize=13, color="#071024",
            ha="center", va="center", fontweight="bold", alpha=a * p,
            zorder=5)
    ax.text(0.5, 0.335,
            f"生成于 {datetime.now():%Y-%m-%d %H:%M} · 数据来自本地快照",
            fontsize=11.5, color=DIM, ha="center", alpha=a * p, zorder=4)
    ax.add_patch(Rectangle((0.455, 0.20), 0.030, 0.030, facecolor=CYAN,
                           edgecolor="none", alpha=a * 0.9, zorder=3))
    ax.add_patch(Rectangle((0.478, 0.223), 0.030, 0.030, facecolor="none",
                           edgecolor=PINK, linewidth=1.6, alpha=a, zorder=3))
    _sparkles(ax, [(0.30, 0.62, 12, 0.8), (0.70, 0.36, 10, 2.9),
                   (0.52, 0.72, 8, 4.6)], t, a)


# 流体几何版场景表(在 classic 基础上多一幕"净增量·近期视频")
SCENES_FLUID = [("title", 3.0), ("overview", 6.5), ("trend", 10.0),
                ("gains_videos", 9.0), ("gains_recent", 8.0),
                ("gains_games", 8.0), ("bars", 10.0), ("end", 3.0)]

_DRAW = {"title": _sc_title, "overview": _sc_overview, "trend": _sc_trend,
         "gains_videos": _sc_gains_videos, "gains_recent": _sc_gains_recent,
         "gains_games": _sc_gains_games,
         "bars": _sc_bars, "end": _sc_end}


def _race_timeline_local(data, n, hold=12, hold_end=90):
    """条形竞跑时间轴(video.py 版的参数化本地实现):
    hold 开头定格帧数 / hold_end 结尾定格帧数可由场景时长与扫描占比推导."""
    tops = data["tops"]
    t0, t1 = _time_axis(data)
    sweep = max(1, n - hold - hold_end)
    top_y, bot_y = 0.80, 0.185
    step = (top_y - bot_y) / len(tops)
    frames, ys = [], None
    for i in range(n):
        p = 0.0 if i < hold else (1.0 if i >= n - hold_end
                                  else (i - hold) / sweep)
        t = t0 + (t1 - t0) * p
        vals = sorted(((it, _interp_value(it["pts"], t)) for it in tops),
                      key=lambda x: -x[1])
        target = {it["bvid"]: top_y - (rank + 0.5) * step
                  for rank, (it, _) in enumerate(vals)}
        if ys is None:
            ys = dict(target)
        else:
            for k in ys:
                ys[k] += (target[k] - ys[k]) * 0.28
        frames.append({"t": t, "vals": vals, "ys": dict(ys)})
    return frames


def make_video(db, cfg, ts_from, ts_to, out_path=None, fps=30, opts=None):
    """生成流体几何风数据报告视频, 返回 MP4 路径(不覆盖既有文件).

    opts 可调参数(全部可选):
    - scene_seconds: {场景名: 秒} 覆盖各幕时长(0 则跳过该幕)
    - sweep_frac:    每幕中"动画推进"所占比例(0.5~0.95, 越小越快/定格越久)
    - trend_top / race_top: 走势与竞跑收录的视频数
    - title / subtitle: 片头主/副标题文案
    """
    setup_font(cfg["charts"].get("font"))
    try:
        import imageio_ffmpeg
        plt.rcParams["animation.ffmpeg_path"] = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    opts = opts or {}
    scenes_o = dict(opts.get("scenes") or {})
    # 池子取各幕收录数上限, 供各幕自行精选
    pool_t = max(8, int((scenes_o.get("trend") or {}).get("top") or 8),
                 int((scenes_o.get("gains_videos") or {}).get("top") or 8))
    pool_r = max(10, int((scenes_o.get("bars") or {}).get("top") or 10))
    data = collect(db, cfg, ts_from, ts_to, trend_pool=pool_t, tops_pool=pool_r)
    if not data["trend"] and not data["tops"]:
        raise SystemExit("该时段没有可用快照数据, 无法生成视频")

    data["_o"] = {"scenes": scenes_o}
    t_title = ((scenes_o.get("title") or {}).get("title")
               or opts.get("title") or "B站官号数据变化报告")
    t_sub = ((scenes_o.get("title") or {}).get("subtitle")
             or opts.get("subtitle") or "VIEW / SNAPSHOT / LOCAL ARCHIVE")
    data["_title"] = str(t_title)
    data["_subtitle"] = str(t_sub)
    sweep_frac = min(0.95, max(0.4, float(opts.get("sweep_frac") or 0.75)))
    scene_seconds = dict(opts.get("scene_seconds") or {})
    # 各幕也可把时长写在 scenes.<name>.dur 里(GUI JSON 形式), 此处合并
    for k, so in scenes_o.items():
        if isinstance(so, dict) and so.get("dur") is not None:
            scene_seconds[k] = float(so["dur"])
    # 清除可能缓存的旧时间轴(参数变更后必须重算)
    for k in ("_side_trend", "_side_gains", "_race"):
        data.pop(k, None)

    dpi = 150
    fig = plt.figure(figsize=(1920 / dpi, 1080 / dpi), dpi=dpi)
    fig.patch.set_facecolor(BG1)

    bounds, acc = [], 0
    pads = {}
    for name, dur in SCENES_FLUID:
        dur = float(scene_seconds.get(name, dur))
        nf = max(1, int(dur * fps)) if dur > 0 else 0
        pads[name] = max(6, int(nf * (1 - sweep_frac)))
        bounds.append((name, acc, nf))
        acc += nf
    total = acc
    data["_pads"] = pads

    def update(frame):
        fig.clear()
        fig.patch.set_facecolor(BG1)
        for name, start, nf in bounds:
            if nf and start <= frame < start + nf:
                _DRAW[name](fig, frame - start, nf, data)
                break
        return []

    anim = FuncAnimation(fig, update, frames=total, interval=1000 // fps)
    writer = FFMpegWriter(fps=fps, codec="libx264", bitrate=9000,
                          extra_args=["-pix_fmt", "yuv420p",
                                      "-movflags", "+faststart"])
    if not out_path:
        outdir = os.path.join("output", "videos")
        os.makedirs(outdir, exist_ok=True)
        base = (f"report_fluid_{ts_from[:10].replace('-', '')}-"
                f"{ts_to[:10].replace('-', '')}")
        out_path = os.path.join(outdir, base + ".mp4")
        k = 1
        while os.path.exists(out_path):
            k += 1
            out_path = os.path.join(outdir, f"{base}_{k}.mp4")
    log.info("开始渲染流体几何风视频: %d 帧 @%dfps (scenes=%s sweep=%.2f) → %s",
             total, fps, dict(scene_seconds), sweep_frac, out_path)
    anim.save(out_path, writer=writer)
    plt.close(fig)
    log.info("视频已生成: %s", out_path)
    return os.path.abspath(out_path)
