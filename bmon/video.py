"""数据变化可视化视频生成: 指定时期内播放量变化的动效短片.

matplotlib 逐帧渲染 + imageio-ffmpeg 自带编码器输出 1920x1080 H.264 MP4.
场景结构: 片头 → 总览(合计+分游戏) → 播放量走势折线 → 净增量走势(视频+游戏合计)
          → 逐日增量条形竞跑(带日期轴) → 片尾.
"""
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FFMpegWriter, FuncAnimation
from matplotlib.patches import FancyBboxPatch, Rectangle

from .charts import account_style, setup_font
from .util import fmt_num

log = logging.getLogger("bmon.video")

TSFMT = "%Y-%m-%d %H:%M:%S"

# 深色主题(与 Web GUI 同源, 视频侧微调更柔和)
BG = "#0f1319"
PANEL = "#151a22"
BORDER = "#242b37"
GRID = "#1c222d"
TEXT = "#e9edf4"
DIM = "#8593a8"
CYAN = "#23ade3"
GREEN = "#3ecf8e"
GOLD = "#e6b450"

# (场景名, 时长秒)
SCENES = [("title", 3.0), ("overview", 6.5), ("trend", 10.0),
          ("gains_videos", 9.0), ("gains_games", 8.0),
          ("bars", 10.0), ("end", 3.0)]


def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _fade(i, n, fin=15, fout=10):
    return min(1.0, i / fin) * min(1.0, (n - i) / fout)


def _strip_game(s):
    """去掉标题开头的《游戏名》前缀(可多个), 避免占用展示宽度."""
    return re.sub(r"^(《[^》]*》[\s\-—·|]*)+", "", s).strip() or s


def _wrap_lines(s, disp_width):
    """按显示宽度折行(中文计2), 不丢字."""
    lines, cur, w = [], "", 0
    for ch in s:
        c = 2 if unicodedata.east_asian_width(ch) in "FW" else 1
        if w + c > disp_width:
            lines.append(cur)
            cur, w = ch, c
        else:
            cur += ch
            w += c
    if cur:
        lines.append(cur)
    return lines


def _wrap2(title, disp_width=26, max_lines=2):
    """把标题折成不超过 max_lines 行完整显示(不截断);
    长标题自动放宽行宽重折, 宁可宽一点也不丢字."""
    s = _strip_game(str(title).strip())
    lines = []
    for w in (disp_width, disp_width + 6, disp_width + 12, disp_width + 18):
        lines = _wrap_lines(s, w)
        if len(lines) <= max_lines:
            break
    return "\n".join(lines)


def _short(s, width=13):
    s = _strip_game(str(s).strip())
    out = ""
    for ch in s:
        if len(out) >= width:
            return out + "…"
        out += ch
    return out


# ---------- 数据准备 ----------
def _interp_value(pts, t):
    """快照序列在时刻 t 的线性插值(用于逐日变化的平滑动画)."""
    if t <= pts[0][0]:
        return pts[0][1]
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        if t <= tb:
            span = (tb - ta).total_seconds() or 1.0
            return va + (vb - va) * (t - ta).total_seconds() / span
    return pts[-1][1]


def collect(db, cfg, ts_from, ts_to):
    """汇总视频: 趋势序列 / 增量Top / 总览数字 / 分游戏明细."""
    rows = db.videos_with_stats()
    meta = {r["bvid"]: r for r in rows}
    ordered, labels, colors = account_style(cfg, rows)

    snaps = db.snapshots_between(ts_from, ts_to)
    base = db.con.execute(
        "SELECT bvid, view, MAX(ts) AS ts FROM snapshots "
        "WHERE ts<=? AND view IS NOT NULL GROUP BY bvid", (ts_from,)).fetchall()

    series = {}
    for b in base:
        if b["bvid"] in meta:
            series[b["bvid"]] = [(datetime.strptime(ts_from, TSFMT), b["view"])]
    for s in snaps:
        if s["view"] is None or s["bvid"] not in meta:
            continue
        series.setdefault(s["bvid"], []).append(
            (datetime.strptime(s["ts"], TSFMT), s["view"]))

    items = []
    for bvid, pts in series.items():
        if len(pts) < 2:
            continue
        pts.sort(key=lambda x: x[0])
        m = meta[bvid]
        items.append({
            "bvid": bvid, "title": m.get("title") or bvid,
            "mid": m.get("mid"), "account": labels.get(m.get("mid"), ""),
            "color": colors.get(m.get("mid"), "#999"),
            "pts": pts, "start": pts[0][1], "end": pts[-1][1],
            "growth": max(0, pts[-1][1] - pts[0][1]),
        })

    trend = sorted([it for it in items if it["end"] and it["growth"] > 0],
                   key=lambda x: -x["end"])[:8]
    tops = sorted([it for it in items if it["growth"] > 0],
                  key=lambda x: -x["growth"])[:10]
    summary = {
        "videos": len(rows),
        "views": sum(r.get("latest_view") or 0 for r in rows),
        "growth": sum(it["growth"] for it in items),
    }

    # 分游戏明细: 视频数 / 累计播放 / 本期增量
    accounts = []
    for mid in ordered:
        acc_rows = [r for r in rows if r.get("mid") == mid]
        if not acc_rows:
            continue
        accounts.append({
            "mid": mid, "name": labels.get(mid, str(mid)),
            "color": colors.get(mid, "#999"),
            "videos": len(acc_rows),
            "views": sum(r.get("latest_view") or 0 for r in acc_rows),
            "growth": sum(it["growth"] for it in items if it["mid"] == mid),
        })

    # 各游戏合计净增量曲线(时间轴采样)
    t0d, t1d = datetime.strptime(ts_from, TSFMT), datetime.strptime(ts_to, TSFMT)
    if t1d <= t0d:
        t1d = t0d + timedelta(days=1)
    samples = [t0d + (t1d - t0d) * k / 120 for k in range(121)]
    acc_gains = []
    for acc in accounts:
        its = [it for it in items if it["mid"] == acc["mid"]]
        if not its:
            continue
        pts = [(t, sum(_interp_value(it["pts"], t) - it["start"] for it in its))
               for t in samples]
        acc_gains.append({**acc, "pts": pts, "end": pts[-1][1]})
    acc_gains.sort(key=lambda x: -x["end"])

    return {"trend": trend, "tops": tops, "summary": summary,
            "accounts": accounts, "acc_gains": acc_gains,
            "labels": labels, "colors": colors, "ordered": ordered,
            "ts_from": ts_from, "ts_to": ts_to}


# ---------- 画布工具 ----------
def _full_ax(fig):
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return ax


def _panel(ax, x, y, w, h, alpha=1.0, zorder=1):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0,rounding_size=0.011",
                                facecolor=PANEL, edgecolor=BORDER, linewidth=1,
                                alpha=alpha, transform=ax.transAxes,
                                mutation_aspect=16 / 9, zorder=zorder))


def _header(ax, title, sub, period, alpha=1.0):
    ax.text(0.07, 0.932, title, fontsize=22, color=TEXT, fontweight="bold",
            alpha=alpha)
    ax.add_patch(Rectangle((0.07, 0.903), 0.042, 0.0042, facecolor=CYAN,
                           edgecolor="none", alpha=alpha, transform=ax.transAxes))
    if sub:
        ax.text(0.07, 0.880, sub, fontsize=12, color=DIM, alpha=alpha)
    if period:
        ax.text(0.93, 0.932, period, fontsize=11.5, color=DIM, ha="right",
                alpha=alpha)


def _period_str(data):
    return f"{data['ts_from'][:10]} ~ {data['ts_to'][:10]}"


# ---------- 场景 ----------
def _sc_title(fig, i, n, data):
    ax = _full_ax(fig)
    p, a = _ease(i / max(1, n - 1)), _fade(i, n)
    ax.text(0.5, 0.615, "B站官号数据变化报告", fontsize=50, color=TEXT,
            ha="center", fontweight="bold", alpha=a)
    ax.plot([0.5 - 0.10 * p, 0.5 + 0.10 * p], [0.565, 0.565], color=CYAN,
            lw=2.2, alpha=a)
    ax.text(0.5, 0.495, _period_str(data), fontsize=23, color=CYAN,
            ha="center", alpha=a * p)
    ax.text(0.5, 0.425, "播放量 · 快照数据 · 本地监测", fontsize=13.5,
            color=DIM, ha="center", alpha=a * p)
    ax.text(0.5, 0.09, "bmon · bilibili-monitor", fontsize=11.5, color=DIM,
            ha="center", alpha=a * 0.7)


def _sc_overview(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    p = _ease(i / max(1, n - 25))
    _header(ax, "总览", "全账号合计与分游戏明细", _period_str(data), alpha=a)
    s = data["summary"]
    cards = [
        ("监测视频", fmt_num(int(s["videos"] * p)), TEXT),
        ("累计播放", fmt_num(int(s["views"] * p)), TEXT),
        ("本期播放增量", "+" + fmt_num(int(s["growth"] * p)), GREEN),
    ]
    for k, (label, val, color) in enumerate(cards):
        x = 0.07 + k * 0.30
        _panel(ax, x, 0.635, 0.27, 0.20, alpha=a)
        ax.text(x + 0.135, 0.790, label, fontsize=13, color=DIM, ha="center",
                alpha=a, transform=ax.transAxes)
        ax.text(x + 0.135, 0.700, val, fontsize=30, color=color, ha="center",
                fontweight="bold", alpha=a, transform=ax.transAxes)
    ax.text(0.07, 0.578, "分游戏明细", fontsize=13, color=DIM, alpha=a)
    for k, acc in enumerate(data["accounts"][:3]):
        y = 0.435 - k * 0.128
        _panel(ax, 0.07, y, 0.86, 0.105, alpha=a)
        ax.plot([0.098], [y + 0.0525], "o", color=acc["color"], ms=9, alpha=a,
                transform=ax.transAxes)
        ax.text(0.118, y + 0.0525, acc["name"], fontsize=15, color=TEXT,
                va="center", fontweight="bold", alpha=a, transform=ax.transAxes)
        cols = [("视频数", fmt_num(int(acc["videos"] * p)), TEXT),
                ("累计播放", fmt_num(int(acc["views"] * p)), TEXT),
                ("本期增量", "+" + fmt_num(int(acc["growth"] * p)), GREEN)]
        for c, (clabel, cval, ccolor) in enumerate(cols):
            cx = 0.50 + c * 0.155
            ax.text(cx, y + 0.072, clabel, fontsize=10, color=DIM,
                    ha="center", alpha=a, transform=ax.transAxes)
            ax.text(cx, y + 0.026, cval, fontsize=16, color=ccolor,
                    ha="center", fontweight="bold", alpha=a,
                    transform=ax.transAxes)
    # 底部时间轴(与数字滚动进度同步推进)
    t0, t1 = _time_axis(data)
    tl_y = 0.105
    ax.plot([0.07, 0.93], [tl_y, tl_y], color=BORDER, lw=1.5, alpha=a)
    span = (t1 - t0).total_seconds()
    for day in [t0] + _day_ticks(t0, t1) + [t1]:
        gx = 0.07 + 0.86 * (day - t0).total_seconds() / span
        ax.plot([gx, gx], [tl_y, tl_y + 0.010], color=BORDER, lw=1.2, alpha=a)
        if day != t1:
            ax.text(gx, tl_y - 0.030, day.strftime("%m-%d"), fontsize=9,
                    color=DIM, ha="center", alpha=a)
    px = 0.07 + 0.86 * p
    t_cur = t0 + (t1 - t0) * p
    ax.plot([0.07, px], [tl_y, tl_y], color=CYAN, lw=2.5, alpha=a, zorder=4)
    ax.plot([px], [tl_y], "o", color=CYAN, ms=6, alpha=a, zorder=4)
    ax.text(px, tl_y + 0.022, t_cur.strftime("%m-%d"), fontsize=10, color=CYAN,
            ha="center", fontweight="bold", alpha=a)


def _time_axis(data):
    t0 = datetime.strptime(data["ts_from"], TSFMT)
    t1 = datetime.strptime(data["ts_to"], TSFMT)
    if t1 <= t0:
        t1 = t0 + timedelta(days=1)
    return t0, t1


def _day_ticks(t0, t1):
    day = (t0 + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    out = []
    while day < t1:
        out.append(day)
        day += timedelta(days=1)
    return out


def _sc_trend(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _header(ax, "播放量走势", "监测期内变化最显著的 Top8 视频 · 快照折线",
            _period_str(data), alpha=a)
    trend = data["trend"]
    if not trend:
        ax.text(0.5, 0.45, "本期暂无趋势数据", fontsize=20, color=DIM,
                ha="center", alpha=a)
        return
    t0, t1 = _time_axis(data)
    x0, x1, y0, y1 = 0.09, 0.70, 0.14, 0.80
    _panel(ax, 0.07, 0.12, 0.65, 0.70, alpha=a)
    vmin = min(v for it in trend for _, v in it["pts"])
    vmax = max(v for it in trend for _, v in it["pts"])
    lo = max(0, vmin - (vmax - vmin) * 0.12)
    hi = vmax + (vmax - vmin) * 0.08 or 1

    def tx(t):
        return x0 + (x1 - x0) * (t - t0).total_seconds() / (t1 - t0).total_seconds()

    def vy(v):
        return y0 + (y1 - y0) * (v - lo) / (hi - lo)

    ax.plot([x0, x1], [y0, y0], color=BORDER, lw=1.2, alpha=a)
    for day in _day_ticks(t0, t1):
        gx = tx(day)
        ax.plot([gx, gx], [y0, y1], color=GRID, lw=1, alpha=a)
        ax.text(gx, y0 - 0.032, day.strftime("%m-%d"), fontsize=10.5,
                color=DIM, ha="center", alpha=a)
    for frac in (0.0, 0.33, 0.66, 1.0):
        v = lo + (hi - lo) * frac
        ax.text(x0 - 0.012, vy(v), fmt_num(int(v)), fontsize=10, color=DIM,
                ha="right", va="center", alpha=a)
        if frac > 0:
            ax.plot([x0, x1], [vy(v)] * 2, color=GRID, lw=1, alpha=a)

    sweep = t0 + (t1 - t0) * min(1.0, i / max(1, n - 90))
    for it in trend:
        f0 = it["pts"][0][0]
        if sweep < f0:
            continue
        # 快照点间线性插值采样, 折线随扫描平滑生长(不逐点跳出)
        xs, ys = [], []
        for k in range(121):
            t = f0 + (sweep - f0) * k / 120
            xs.append(tx(t))
            ys.append(vy(_interp_value(it["pts"], t)))
        ax.plot(xs, ys, color=it["color"], lw=2.8, alpha=a * 0.95,
                solid_capstyle="round", zorder=3)
        mp = [(t, v) for t, v in it["pts"] if t <= sweep]
        ax.plot([tx(t) for t, _ in mp], [vy(v) for _, v in mp], "o",
                color=it["color"], ms=5, markerfacecolor=it["color"],
                markeredgecolor=BG, markeredgewidth=0.8, alpha=a, zorder=4)
    if sweep < t1:
        gx = tx(sweep)
        ax.plot([gx, gx], [y0, y1], color=DIM, lw=1, ls="--", alpha=a * 0.45)

    for k, it in enumerate(trend):
        sy = 0.78 - k * 0.082
        cur = next((v for t, v in reversed(it["pts"]) if t <= sweep), None)
        ax.plot([0.735], [sy], "o", color=it["color"], ms=7, alpha=a,
                transform=ax.transAxes)
        ax.text(0.755, sy, _wrap2(it["title"], 24), fontsize=9.5, color=TEXT,
                va="center", linespacing=1.15, alpha=a, transform=ax.transAxes)
        ax.text(0.985, sy, fmt_num(cur) if cur else "—", fontsize=11.5,
                color=it["color"], va="center", ha="right",
                fontweight="bold", alpha=a, transform=ax.transAxes)


def _sc_gains_videos(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _header(ax, "净增量走势 · 视频", "变化最显著的 Top8 视频各自净增量",
            _period_str(data), alpha=a)
    trend = data["trend"]
    if not trend:
        ax.text(0.5, 0.45, "本期暂无增量数据", fontsize=20, color=DIM,
                ha="center", alpha=a)
        return
    t0, t1 = _time_axis(data)
    x0, x1, y0, y1 = 0.09, 0.70, 0.14, 0.80
    _panel(ax, 0.07, 0.12, 0.65, 0.70, alpha=a)
    gmax = max(it["growth"] for it in trend) * 1.08 or 1

    def tx(t):
        return x0 + (x1 - x0) * (t - t0).total_seconds() / (t1 - t0).total_seconds()

    def vy(v):
        return y0 + (y1 - y0) * max(0.0, v) / gmax

    ax.plot([x0, x1], [y0, y0], color=BORDER, lw=1.2, alpha=a)
    for day in _day_ticks(t0, t1):
        gx = tx(day)
        ax.plot([gx, gx], [y0, y1], color=GRID, lw=1, alpha=a)
        ax.text(gx, y0 - 0.032, day.strftime("%m-%d"), fontsize=10.5,
                color=DIM, ha="center", alpha=a)
    for frac in (0.33, 0.66, 1.0):
        ax.text(x0 - 0.012, vy(gmax * frac), fmt_num(int(gmax * frac)),
                fontsize=10, color=DIM, ha="right", va="center", alpha=a)
        ax.plot([x0, x1], [vy(gmax * frac)] * 2, color=GRID, lw=1, alpha=a)

    sweep = t0 + (t1 - t0) * min(1.0, i / max(1, n - 90))
    for it in trend:
        f0 = it["pts"][0][0]
        if sweep < f0:
            continue
        xs, ys = [], []
        for k in range(121):
            t = f0 + (sweep - f0) * k / 120
            xs.append(tx(t))
            ys.append(vy(_interp_value(it["pts"], t) - it["start"]))
        ax.plot(xs, ys, color=it["color"], lw=2.6, alpha=a * 0.95,
                solid_capstyle="round", zorder=3)
        mp = [(t, v - it["start"]) for t, v in it["pts"] if t <= sweep]
        ax.plot([tx(t) for t, _ in mp], [vy(v) for _, v in mp], "o",
                color=it["color"], ms=5, markerfacecolor=it["color"],
                markeredgecolor=BG, markeredgewidth=0.8, alpha=a, zorder=4)
    if sweep < t1:
        gx = tx(sweep)
        ax.plot([gx, gx], [y0, y1], color=DIM, lw=1, ls="--", alpha=a * 0.45)

    for k, it in enumerate(trend):
        sy = 0.78 - k * 0.082
        cur = next((v - it["start"] for t, v in reversed(it["pts"]) if t <= sweep), 0)
        ax.plot([0.735], [sy], "o", color=it["color"], ms=7, alpha=a,
                transform=ax.transAxes)
        ax.text(0.755, sy, _wrap2(it["title"], 24), fontsize=9.5, color=TEXT,
                va="center", linespacing=1.15, alpha=a, transform=ax.transAxes)
        ax.text(0.985, sy, "+" + fmt_num(max(0, int(cur))), fontsize=11.5,
                color=GREEN, va="center", ha="right",
                fontweight="bold", alpha=a, transform=ax.transAxes)


def _sc_gains_games(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _header(ax, "净增量走势 · 分游戏", "三个游戏全部监测视频的合计净增量",
            _period_str(data), alpha=a)
    acc_gains = data["acc_gains"]
    if not acc_gains:
        ax.text(0.5, 0.45, "本期暂无增量数据", fontsize=20, color=DIM,
                ha="center", alpha=a)
        return
    t0, t1 = _time_axis(data)
    x0, x1, y0, y1 = 0.09, 0.70, 0.14, 0.80
    _panel(ax, 0.07, 0.12, 0.65, 0.70, alpha=a)
    gmax = max(g["end"] for g in acc_gains) * 1.10 or 1

    def tx(t):
        return x0 + (x1 - x0) * (t - t0).total_seconds() / (t1 - t0).total_seconds()

    def vy(v):
        return y0 + (y1 - y0) * max(0.0, v) / gmax

    ax.plot([x0, x1], [y0, y0], color=BORDER, lw=1.2, alpha=a)
    for day in _day_ticks(t0, t1):
        gx = tx(day)
        ax.plot([gx, gx], [y0, y1], color=GRID, lw=1, alpha=a)
        ax.text(gx, y0 - 0.032, day.strftime("%m-%d"), fontsize=10.5,
                color=DIM, ha="center", alpha=a)
    for frac in (0.33, 0.66, 1.0):
        ax.text(x0 - 0.012, vy(gmax * frac), fmt_num(int(gmax * frac)),
                fontsize=10, color=DIM, ha="right", va="center", alpha=a)
        ax.plot([x0, x1], [vy(gmax * frac)] * 2, color=GRID, lw=1, alpha=a)

    sweep = t0 + (t1 - t0) * min(1.0, i / max(1, n - 90))
    for g in acc_gains:
        pts = [(t, v) for t, v in g["pts"] if t <= sweep]
        if not pts:
            continue
        xs, ys = [tx(t) for t, _ in pts], [vy(v) for _, v in pts]
        ax.plot(xs, ys, color=g["color"], lw=3.4, alpha=a,
                marker="o", ms=6, markerfacecolor=g["color"],
                markeredgecolor=BG, markeredgewidth=0.8,
                solid_capstyle="round", zorder=4)
    if sweep < t1:
        gx = tx(sweep)
        ax.plot([gx, gx], [y0, y1], color=DIM, lw=1, ls="--", alpha=a * 0.45)

    p = _ease(i / max(1, n - 25))
    for k, g in enumerate(acc_gains[:3]):
        sy = 0.74 - k * 0.115
        cur = next((v for t, v in reversed(g["pts"]) if t <= sweep), 0)
        _panel(ax, 0.73, sy - 0.045, 0.255, 0.09, alpha=a)
        ax.plot([0.752], [sy], "o", color=g["color"], ms=9, alpha=a,
                transform=ax.transAxes)
        ax.text(0.772, sy, g["name"], fontsize=12.5, color=TEXT, va="center",
                fontweight="bold", alpha=a, transform=ax.transAxes)
        ax.text(0.965, sy, "+" + fmt_num(int(cur * p)), fontsize=14,
                color=GREEN, va="center", ha="right", fontweight="bold",
                alpha=a, transform=ax.transAxes)


def _race_timeline(data, n):
    """预计算条形竞跑时间轴: 每帧各视频的当前播放量、排名与平滑后的纵向位置."""
    tops = data["tops"]
    t0, t1 = _time_axis(data)
    hold, hold_end = 12, 90          # 开头定格0.4s, 结尾定格3s
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


def _bar_axis(tops):
    """播放量横轴范围: 最高者占满、其余等比缩放;
    若各视频播放量相对差异过小(不足最高值的45%), 自动抬升轴起点(截断轴)放大差异."""
    vmax = max(it["end"] for it in tops) or 1
    vmin = min(it["start"] for it in tops)
    if vmax <= 0:
        return 0, 1
    if (vmax - vmin) / vmax >= 0.45 or vmin <= 0:
        axis_min = 0
    else:
        step = 10 ** max(5, len(str(int(vmin))) - 2)
        axis_min = int(vmin * 0.995 // step) * step
    return axis_min, vmax * 1.04


def _sc_bars(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _header(ax, "播放量竞跑 Top10",
            "条形长度=当前播放量(横轴) · 最高者占满, 其余等比 · 条尾为当日新增",
            _period_str(data), alpha=a)
    tops = data["tops"]
    if not tops:
        ax.text(0.5, 0.45, "本期暂无增量数据", fontsize=20, color=DIM,
                ha="center", alpha=a)
        return
    race = data.get("_race")
    if race is None:
        race = data["_race"] = _race_timeline(data, n)
    fr = race[min(i, len(race) - 1)]
    t0, t1 = _time_axis(data)
    left, right = 0.28, 0.80
    top_y, bot_y = 0.80, 0.185
    step = (top_y - bot_y) / len(tops)
    axis_min, axis_max = _bar_axis(tops)

    def bw(v):
        return (right - left) * max(0.0, v - axis_min) / (axis_max - axis_min)

    # 顶部播放量横轴刻度 + 竖向网格
    for frac in (0.0, 1 / 3, 2 / 3, 1.0):
        val = axis_min + (axis_max - axis_min) * frac
        gx = left + (right - left) * frac
        ax.plot([gx, gx], [bot_y, top_y], color=GRID, lw=1, alpha=a)
        ax.text(gx, 0.845, fmt_num(int(val)), fontsize=9.5, color=DIM,
                ha="center", alpha=a)
    if axis_min > 0:
        ax.text(left - 0.012, 0.845, "//", fontsize=9.5, color=DIM,
                ha="right", alpha=a)
        ax.text(0.93, 0.880, "轴起点已抬升以突出差异", fontsize=9,
                color=DIM, ha="right", alpha=a * 0.85)
    ax.text(0.985, 0.845, "当前播放量", fontsize=9.5, color=DIM, ha="right",
            alpha=a)

    for rank, (it, v) in enumerate(fr["vals"]):
        y = fr["ys"][it["bvid"]]
        w = max(0.0001, bw(v))
        ax.text(left - 0.018, y, _wrap2(it["title"], 30), fontsize=8.8,
                color=TEXT, ha="right", va="center", linespacing=1.1,
                alpha=a, transform=ax.transAxes, zorder=3)
        ax.add_patch(Rectangle((left, y - step * 0.28), w, step * 0.56,
                               facecolor=it["color"], edgecolor="none",
                               alpha=a * 0.92, transform=ax.transAxes, zorder=3))
        gain = int(max(0, v - it["start"]))
        if gain > 0:
            ax.text(left + w + 0.010, y, "+" + fmt_num(gain),
                    fontsize=10.5, color=GOLD if rank < 3 else TEXT, va="center",
                    fontweight="bold" if rank < 3 else "normal", alpha=a,
                    transform=ax.transAxes, zorder=3)
        ax.text(0.985, y, fmt_num(int(v)), fontsize=10.5,
                color=DIM, va="center", ha="right", alpha=a,
                transform=ax.transAxes, zorder=3)

    # 底部日期轴(约一周): 基准线 + 每日刻度 + 当前进度
    ax.plot([left, right], [0.115, 0.115], color=BORDER, lw=1.5, alpha=a)
    span = (t1 - t0).total_seconds()
    for day in [t0] + _day_ticks(t0, t1) + [t1]:
        gx = left + (right - left) * (day - t0).total_seconds() / span
        ax.plot([gx, gx], [0.115, 0.125], color=BORDER, lw=1.2, alpha=a)
        if day != t1:
            ax.text(gx, 0.086, day.strftime("%m-%d"), fontsize=9, color=DIM,
                    ha="center", alpha=a)
    px = left + (right - left) * (fr["t"] - t0).total_seconds() / span
    ax.plot([left, px], [0.115, 0.115], color=CYAN, lw=2.5, alpha=a, zorder=4)
    ax.plot([px], [0.115], "o", color=CYAN, ms=6, alpha=a, zorder=4)
    ax.text(px, 0.140, fr["t"].strftime("%m-%d"), fontsize=10, color=CYAN,
            ha="center", fontweight="bold", alpha=a)

    # 图例
    seen, lx = [], 0.07
    for it in tops:
        if it["account"] in seen:
            continue
        seen.append(it["account"])
        ax.plot([lx], [0.052], "s", color=it["color"], ms=7.5, alpha=a,
                transform=ax.transAxes)
        ax.text(lx + 0.013, 0.052, it["account"], fontsize=10.5, color=DIM,
                va="center", alpha=a, transform=ax.transAxes)
        lx += 0.013 + 0.015 * len(it["account"]) + 0.028


def _sc_end(fig, i, n, data):
    ax = _full_ax(fig)
    p, a = _ease(i / max(1, n - 1)), _fade(i, n)
    ax.text(0.5, 0.58, "本期报告 · 完", fontsize=38, color=TEXT, ha="center",
            fontweight="bold", alpha=a)
    ax.plot([0.5 - 0.08 * p, 0.5 + 0.08 * p], [0.538, 0.538], color=CYAN,
            lw=2.2, alpha=a)
    ax.text(0.5, 0.465, _period_str(data), fontsize=17, color=CYAN,
            ha="center", alpha=a * p)
    ax.text(0.5, 0.395, f"生成于 {datetime.now():%Y-%m-%d %H:%M} · 数据来自本地快照",
            fontsize=12.5, color=DIM, ha="center", alpha=a * p)


_DRAW = {"title": _sc_title, "overview": _sc_overview, "trend": _sc_trend,
         "gains_videos": _sc_gains_videos, "gains_games": _sc_gains_games,
         "bars": _sc_bars, "end": _sc_end}


# ---------- 对外入口 ----------
def make_video(db, cfg, ts_from, ts_to, out_path=None, fps=30):
    """生成指定时段的数据变化视频, 返回 MP4 路径."""
    setup_font(cfg["charts"].get("font"))
    try:
        import imageio_ffmpeg
        plt.rcParams["animation.ffmpeg_path"] = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    data = collect(db, cfg, ts_from, ts_to)
    if not data["trend"] and not data["tops"]:
        raise SystemExit("该时段没有可用快照数据, 无法生成视频")

    dpi = 150
    fig = plt.figure(figsize=(1920 / dpi, 1080 / dpi), dpi=dpi)
    fig.patch.set_facecolor(BG)

    bounds, acc = [], 0
    for name, dur in SCENES:
        nf = int(dur * fps)
        bounds.append((name, acc, nf))
        acc += nf
    total = acc

    def update(frame):
        fig.clear()
        fig.patch.set_facecolor(BG)
        for name, start, nf in bounds:
            if frame < start + nf:
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
        out_path = os.path.join(
            outdir, f"report_{ts_from[:10].replace('-', '')}-"
                    f"{ts_to[:10].replace('-', '')}.mp4")
    log.info("开始渲染视频: %d 帧 @%dfps → %s", total, fps, out_path)
    anim.save(out_path, writer=writer)
    plt.close(fig)
    log.info("视频已生成: %s", out_path)
    return os.path.abspath(out_path)
