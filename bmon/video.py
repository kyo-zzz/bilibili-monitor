"""数据变化可视化视频生成: 指定时期内播放量变化的动效短片.

matplotlib 逐帧渲染 + imageio-ffmpeg 自带编码器输出 1920x1080 H.264 MP4.
场景结构: 片头 → 总览数字滚动 → 播放趋势曲线生长 → 本期增量Top条形动画 → 片尾.
"""
import logging
import os
import re
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FFMpegWriter, FuncAnimation

from .charts import account_style, setup_font
from .util import fmt_num

log = logging.getLogger("bmon.video")

TSFMT = "%Y-%m-%d %H:%M:%S"

# 与 Web GUI 同源的深色主题
BG = "#0f1216"
PANEL = "#171b22"
TEXT = "#e7eaf0"
DIM = "#8892a6"
CYAN = "#00a1d6"
PINK = "#fb7299"
GREEN = "#2ecc71"
GOLD = "#f0b429"

# (场景名, 时长秒)
SCENES = [("title", 3.0), ("overview", 5.0), ("trend", 10.0),
          ("bars", 9.0), ("end", 3.0)]


def _ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _fade(i, n, fin=15, fout=10):
    return min(1.0, i / fin) * min(1.0, (n - i) / fout)


def _short(s, width=13):
    s = _strip_game(str(s).strip())
    out = ""
    for ch in s:
        if len(out) >= width:
            return out + "…"
        out += ch
    return out


def _strip_game(s):
    """去掉标题开头的《游戏名》前缀(可多个), 避免占用展示宽度."""
    return re.sub(r"^(《[^》]*》[\s\-—·|]*)+", "", s).strip() or s


# ---------- 数据准备 ----------
def collect(db, cfg, ts_from, ts_to):
    """汇总视频: 趋势序列 / 增量Top / 总览数字."""
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
    return {"trend": trend, "tops": tops, "summary": summary,
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


def _scene_title(ax, text, sub="", alpha=1.0):
    ax.text(0.07, 0.93, text, fontsize=23, color=TEXT, alpha=alpha,
            fontweight="bold")
    ax.plot([0.07, 0.07 + 0.055], [0.905, 0.905], color=CYAN, lw=3, alpha=alpha)
    if sub:
        ax.text(0.07, 0.885, sub, fontsize=12.5, color=DIM, alpha=alpha)


# ---------- 场景 ----------
def _sc_title(fig, i, n, data):
    ax = _full_ax(fig)
    p, a = _ease(i / max(1, n - 1)), _fade(i, n)
    d1 = data["ts_from"][:10]
    d2 = data["ts_to"][:10]
    ax.text(0.5, 0.62, "B站官号数据变化报告", fontsize=52, color=TEXT,
            ha="center", fontweight="bold", alpha=a)
    ax.text(0.5, 0.50, f"{d1}  ~  {d2}", fontsize=24, color=CYAN,
            ha="center", alpha=a * p)
    ax.text(0.5, 0.42, "播放量 · 快照数据 · 本地监测", fontsize=14, color=DIM,
            ha="center", alpha=a * p)
    ax.plot([0.5 - 0.11 * p, 0.5 + 0.11 * p], [0.565, 0.565],
            color=PINK, lw=2.5, alpha=a)
    ax.text(0.5, 0.09, "bmon · bilibili-monitor", fontsize=12, color=DIM,
            ha="center", alpha=a * 0.8)


def _sc_overview(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    p = _ease(i / max(1, n - 25))
    _scene_title(ax, "总览", "本期数据概况", alpha=a)
    s = data["summary"]
    cards = [
        ("监测视频", fmt_num(int(s["videos"] * p)), TEXT),
        ("累计播放", fmt_num(int(s["views"] * p)), TEXT),
        ("本期播放增量", "+" + fmt_num(int(s["growth"] * p)), GREEN),
    ]
    for k, (label, val, color) in enumerate(cards):
        cx = 0.14 + k * 0.27
        ax.add_patch(plt.Rectangle((cx, 0.32), 0.22, 0.34, facecolor=PANEL,
                                   edgecolor="#262c37", linewidth=1.2,
                                   alpha=a, transform=ax.transAxes, zorder=2))
        ax.text(cx + 0.11, 0.585, label, fontsize=15, color=DIM,
                ha="center", alpha=a, transform=ax.transAxes, zorder=3)
        ax.text(cx + 0.11, 0.44, val, fontsize=34, color=color,
                ha="center", fontweight="bold", alpha=a,
                transform=ax.transAxes, zorder=3)


def _sc_trend(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _scene_title(ax, "播放量走势", "监测期内变化最显著的 Top8 视频 · 快照折线", alpha=a)
    trend = data["trend"]
    if not trend:
        ax.text(0.5, 0.45, "本期暂无趋势数据", fontsize=20, color=DIM,
                ha="center", alpha=a)
        return
    t0 = datetime.strptime(data["ts_from"], TSFMT)
    t1 = datetime.strptime(data["ts_to"], TSFMT)
    if t1 <= t0:
        t1 = t0 + timedelta(days=1)
    x0, x1, y0, y1 = 0.08, 0.70, 0.13, 0.80
    vmax = max(it["end"] for it in trend) * 1.08

    def tx(t):
        return x0 + (x1 - x0) * (t - t0).total_seconds() / (t1 - t0).total_seconds()

    def vy(v):
        return y0 + (y1 - y0) * v / vmax

    # 网格与日期刻度
    ax.plot([x0, x1], [y0, y0], color="#262c37", lw=1.2, alpha=a)
    day = (t0 + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    while day < t1:
        gx = tx(day)
        ax.plot([gx, gx], [y0, y1], color="#1d222b", lw=1, alpha=a)
        ax.text(gx, y0 - 0.035, day.strftime("%m-%d"), fontsize=11,
                color=DIM, ha="center", alpha=a)
        day += timedelta(days=1)
    for frac in (0.25, 0.5, 0.75, 1.0):
        ax.text(x0 - 0.012, vy(vmax * frac), fmt_num(int(vmax * frac)),
                fontsize=10.5, color=DIM, ha="right", va="center", alpha=a)
        ax.plot([x0, x1], [vy(vmax * frac)] * 2, color="#1d222b", lw=1, alpha=a)

    # 曲线随时间生长(折线连接快照点)
    sweep = t0 + (t1 - t0) * min(1.0, i / max(1, n - 30))
    for it in trend:
        pts = [(t, v) for t, v in it["pts"] if t <= sweep]
        if len(pts) < 1:
            continue
        xs, ys = [tx(t) for t, _ in pts], [vy(v) for _, v in pts]
        ax.plot(xs, ys, color=it["color"], lw=3.0, alpha=a * 0.95,
                marker="o", ms=5.5, markerfacecolor=it["color"],
                markeredgecolor=BG, markeredgewidth=0.8,
                solid_capstyle="round", zorder=3)
    if sweep < t1:
        gx = tx(sweep)
        ax.plot([gx, gx], [y0, y1], color=DIM, lw=1, ls="--", alpha=a * 0.5)

    # 右侧固定槽位标签(按最终播放量排序)
    for k, it in enumerate(trend):
        sy = 0.78 - k * 0.082
        cur = next((v for t, v in reversed(it["pts"]) if t <= sweep), None)
        ax.plot([0.735], [sy], "o", color=it["color"], ms=7, alpha=a,
                transform=ax.transAxes)
        ax.text(0.755, sy, _short(it["title"], 12), fontsize=11.5, color=TEXT,
                va="center", alpha=a, transform=ax.transAxes)
        ax.text(0.985, sy, fmt_num(cur) if cur else "—", fontsize=12,
                color=it["color"], va="center", ha="right",
                fontweight="bold", alpha=a, transform=ax.transAxes)


def _interp_value(pts, t):
    """快照序列在时刻 t 的线性插值(用于逐日变化的平滑动画)."""
    if t <= pts[0][0]:
        return pts[0][1]
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        if t <= tb:
            span = (tb - ta).total_seconds() or 1.0
            return va + (vb - va) * (t - ta).total_seconds() / span
    return pts[-1][1]


def _race_timeline(data, n):
    """预计算条形竞跑时间轴: 每帧各视频的累计增量、排名与平滑后的纵向位置."""
    tops = data["tops"]
    t0 = datetime.strptime(data["ts_from"], TSFMT)
    t1 = datetime.strptime(data["ts_to"], TSFMT)
    if t1 <= t0:
        t1 = t0 + timedelta(days=1)
    hold = 12
    sweep = max(1, n - hold * 2)
    top_y, bot_y = 0.80, 0.12
    step = (top_y - bot_y) / len(tops)
    frames, ys = [], None
    for i in range(n):
        p = 0.0 if i < hold else (1.0 if i >= n - hold else (i - hold) / sweep)
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


def _sc_bars(fig, i, n, data):
    ax = _full_ax(fig)
    a = _fade(i, n)
    _scene_title(ax, "本期播放增量 Top10", "逐日播放变化 · 条形竞跑", alpha=a)
    tops = data["tops"]
    if not tops:
        ax.text(0.5, 0.45, "本期暂无增量数据", fontsize=20, color=DIM,
                ha="center", alpha=a)
        return
    race = data.get("_race")
    if race is None:
        race = data["_race"] = _race_timeline(data, n)
    fr = race[min(i, len(race) - 1)]
    vmax = tops[0]["growth"] or 1
    left, right = 0.34, 0.88
    step = (0.80 - 0.12) / len(tops)
    ax.text(0.925, 0.5, fr["t"].strftime("%m-%d"), fontsize=34, color="#1d222b",
            ha="center", va="center", fontweight="bold", alpha=a,
            transform=ax.transAxes, zorder=1)
    for rank, (it, v) in enumerate(fr["vals"]):
        y = fr["ys"][it["bvid"]]
        w = max(0.0001, (right - left) * v / vmax)
        ax.add_patch(plt.Rectangle((left, y - step * 0.30), w, step * 0.60,
                                   facecolor=it["color"], edgecolor="none",
                                   alpha=a * 0.92, transform=ax.transAxes, zorder=3))
        ax.text(left - 0.045, y, _short(it["title"], 13), fontsize=11.5,
                color=TEXT, ha="right", va="center", alpha=a,
                transform=ax.transAxes, zorder=3)
        ax.text(left + w + 0.012, y, fmt_num(int(v)),
                fontsize=12, color=GOLD if rank < 3 else TEXT, va="center",
                fontweight="bold" if rank < 3 else "normal", alpha=a,
                transform=ax.transAxes, zorder=3)
    # 图例
    seen, lx = [], 0.34
    for it in tops:
        if it["account"] in seen:
            continue
        seen.append(it["account"])
        ax.plot([lx], [0.052], "s", color=it["color"], ms=8, alpha=a,
                transform=ax.transAxes)
        ax.text(lx + 0.014, 0.052, it["account"], fontsize=11.5, color=DIM,
                va="center", alpha=a, transform=ax.transAxes)
        lx += 0.014 + 0.016 * len(it["account"]) + 0.03


def _sc_end(fig, i, n, data):
    ax = _full_ax(fig)
    p, a = _ease(i / max(1, n - 1)), _fade(i, n)
    ax.text(0.5, 0.58, "本期报告 · 完", fontsize=40, color=TEXT, ha="center",
            fontweight="bold", alpha=a)
    ax.plot([0.5 - 0.09 * p, 0.5 + 0.09 * p], [0.535, 0.535], color=CYAN,
            lw=2.5, alpha=a)
    ax.text(0.5, 0.46, f"{data['ts_from'][:10]} ~ {data['ts_to'][:10]}",
            fontsize=18, color=CYAN, ha="center", alpha=a * p)
    ax.text(0.5, 0.38, f"生成于 {datetime.now():%Y-%m-%d %H:%M} · 数据来自本地快照",
            fontsize=13, color=DIM, ha="center", alpha=a * p)


_DRAW = {"title": _sc_title, "overview": _sc_overview, "trend": _sc_trend,
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
