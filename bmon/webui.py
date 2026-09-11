"""本地 Web GUI: 总览/视频筛选/趋势/快照查询/采集控制.

仅监听 127.0.0.1, 无需联网鉴权, 数据全部来自本地 SQLite.
采集等动作通过子进程调用 main.py, 与命令行行为完全一致.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_from_directory)

from . import config as cfgmod
from . import filters as filters_mod
from . import scheduler as schedmod
from .storage import Database
from .util import fmt_num

log = logging.getLogger("bmon.webui")

MAIN_PY = os.path.join(cfgmod.ROOT, "main.py")
_procs = {"fetch": None, "chart": None, "video": None}


def _spawn(cfg, key, args):
    """以子进程执行 main.py 子命令(GUI按钮/视频生成共用); 已在跑则忽略."""
    p = _procs.get(key)
    if p and p.poll() is None:
        return False
    data_dir = os.path.dirname(cfg["storage"]["db_path"])
    os.makedirs(data_dir, exist_ok=True)
    logf = open(os.path.join(data_dir, "gui_runs.log"), "ab")
    _procs[key] = subprocess.Popen(
        [sys.executable, MAIN_PY] + args, cwd=cfgmod.ROOT,
        stdout=logf, stderr=subprocess.STDOUT)
    log.info("GUI 触发子进程: %s", args)
    return True


def _read_state(cfg):
    path = os.path.join(os.path.dirname(cfg["storage"]["db_path"]), "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _tail(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return "(暂无日志)"


def _parse_time(s):
    s = str(s).strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def create_app(cfg):
    app = Flask("bmon",
                template_folder=os.path.join(cfgmod.ROOT, "templates"),
                static_folder=os.path.join(cfgmod.ROOT, "static"))
    app.config["BMON_CFG"] = cfg
    app.jinja_env.globals["fmt_num"] = fmt_num
    app.jinja_env.globals["growth_of"] = filters_mod.growth_of
    app.jinja_env.globals["now_str"] = lambda: datetime.now().strftime("%Y-%m-%d %H:%M")

    def db():
        return Database(cfg["storage"]["db_path"])

    def acc_color(mid, i):
        from .charts import ACCENT
        return ACCENT[i % len(ACCENT)]

    app.jinja_env.globals["acc_color"] = acc_color

    # ---------- 总览 ----------
    @app.route("/")
    def index():
        con = db()
        rows = con.videos_with_stats()
        con.close()
        per = {}
        for i, r in enumerate(rows):
            p = per.get(r["mid"])
            if p is None:
                p = {"mid": r["mid"], "name": r.get("account") or str(r["mid"]),
                     "videos": 0, "views": 0, "growth": 0,
                     "color": acc_color(r["mid"], len(per))}
                per[r["mid"]] = p
            p["videos"] += 1
            p["views"] += r.get("latest_view") or 0
            p["growth"] += filters_mod.growth_of(r) or 0
        chart_files = []
        outdir = cfg["charts"]["output_dir"]
        if os.path.isdir(outdir):
            chart_files = [f for f in os.listdir(outdir) if f.endswith(".png")]
        state = _read_state(cfg)
        from .charts import scan_chart_groups
        chart_groups = scan_chart_groups(cfg)
        return render_template(
            "index.html", per=list(per.values()), state=state,
            chart_groups=chart_groups, videos_total=len(rows),
            views_total=sum(r.get("latest_view") or 0 for r in rows),
            growth_total=sum(filters_mod.growth_of(r) or 0 for r in rows))

    # ---------- 视频筛选 ----------
    @app.route("/videos")
    def videos():
        args = filters_mod.args_from_dict(request.args, default_limit=0,
                                          default_sort="views")
        con = db()
        rows = filters_mod.apply_filters(con.videos_with_stats(), args)
        con.close()
        page = max(1, _to_int(request.args.get("page"), 1))
        per_page = 30
        total = len(rows)
        rows = rows[(page - 1) * per_page: page * per_page]
        cur = {k: request.args.get(k) for k in
               ("account", "keyword", "since", "until", "min_views", "sort", "asc")
               if request.args.get(k)}
        pages = max(1, (total + per_page - 1) // per_page)
        return render_template("videos.html", rows=rows, total=total, page=page,
                               pages=pages, cur=cur,
                               accounts=cfg.get("accounts") or [])

    @app.route("/export/videos.csv")
    def export_videos():
        args = filters_mod.args_from_dict(request.args, default_limit=0,
                                          default_sort="views")
        con = db()
        rows = filters_mod.apply_filters(con.videos_with_stats(), args)
        con.close()
        return Response(filters_mod.csv_content(rows), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=videos.csv"})

    # ---------- 趋势 ----------
    @app.route("/trend")
    def trend():
        con = db()
        all_rows = con.videos_with_stats()
        con.close()
        top = sorted([r for r in all_rows if r.get("latest_view") is not None],
                     key=lambda r: r["latest_view"], reverse=True)[:300]
        options = sorted([r for r in all_rows if r.get("created_ts")],
                         key=lambda r: r["created_ts"], reverse=True)[:500]
        sel = request.args.get("bvid") or (top[0]["bvid"] if top else "")
        sel2 = request.args.get("bvid2") or ""
        info = next((r for r in all_rows if r.get("bvid") == sel), None)
        info2 = next((r for r in all_rows if r.get("bvid") == sel2), None)
        return render_template("trend.html", options=options, sel=sel, sel2=sel2,
                               info=info, info2=info2)

    @app.route("/api/trend")
    def api_trend():
        bvids = [b for b in (request.args.get("bvid"), request.args.get("bvid2"))
                 if b][:2]
        con = db()
        series = []
        for b in bvids:
            t = con.con.execute("SELECT title, mid FROM videos WHERE bvid=?",
                                (b,)).fetchone()
            snaps = con.con.execute(
                "SELECT ts, view, likes FROM snapshots WHERE bvid=? ORDER BY ts",
                (b,)).fetchall()
            series.append({
                "name": (t["title"][:26] if t else b),
                "points": [[r["ts"], r["view"]] for r in snaps],
                "likes": [[r["ts"], r["likes"]] for r in snaps if r["likes"] is not None],
            })
        con.close()
        return jsonify(series)

    # ---------- 快照查询 ----------
    @app.route("/snapshot")
    def snapshot():
        mode = request.args.get("mode", "single")
        at = request.args.get("at") or datetime.now().strftime("%Y-%m-%d %H:%M")
        frm = request.args.get("from") or ""
        to = request.args.get("to") or datetime.now().strftime("%Y-%m-%d %H:%M")
        rows, error = [], None
        ts_from = ts_to = None
        if mode == "range":
            ts_from = _parse_time(frm)
            ts_to = _parse_time(to)
            if not ts_from or not ts_to:
                error = "时间格式应类似 2026-08-16 12:00"
            elif ts_from >= ts_to:
                error = "起始时间必须早于结束时间"
        else:
            ts_to = _parse_time(at)
            if not ts_to:
                error = "时间格式应类似 2026-08-16 12:00"
        if not error and ts_to:
            args = filters_mod.args_from_dict(request.args, default_limit=0,
                                              default_sort="growth")
            con = db()
            rows = filters_mod.apply_filters(
                con.snapshot_report(ts_to, ts_from), args)
            con.close()
            rows = rows[:500]
        cur = {k: request.args.get(k) for k in
               ("account", "keyword", "min_views", "sort", "asc")
               if request.args.get(k)}
        return render_template(
            "snapshot.html", rows=rows, error=error, mode=mode, at=at,
            frm=frm, to=to, cur=cur, accounts=cfg.get("accounts") or [])

    @app.route("/export/snapshot.csv")
    def export_snapshot():
        mode = request.args.get("mode", "single")
        ts_to = _parse_time(request.args.get("to") or
                            datetime.now().strftime("%Y-%m-%d %H:%M"))
        ts_from = _parse_time(request.args.get("from")) if mode == "range" else None
        if not ts_to:
            abort(400)
        args = filters_mod.args_from_dict(request.args, default_limit=0,
                                          default_sort="growth")
        con = db()
        rows = filters_mod.apply_filters(con.snapshot_report(ts_to, ts_from), args)
        con.close()
        return Response(filters_mod.csv_content(rows, fields=filters_mod.SNAP_CSV_FIELDS),
                        mimetype="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=snapshot.csv"})

    # ---------- 数据中心 ----------
    @app.route("/data")
    def data_page():
        from datetime import timedelta
        gran = request.args.get("gran", "daily")
        if gran not in ("daily", "weekly", "monthly"):
            gran = "daily"
        con = db()
        per_day = con.snapshots_per_day()
        by_account = con.snapshots_by_account()
        sources = con.snapshots_by_source()
        rounds = con.round_sizes(30)
        total = con.snapshot_count()
        total_rounds = con.con.execute(
            "SELECT COUNT(DISTINCT ts) FROM snapshots").fetchone()[0]
        first = con.con.execute("SELECT MIN(ts) FROM snapshots").fetchone()[0]
        last = con.last_snapshot_ts()
        # 缺口日: 覆盖范围内无快照的日期
        gaps = []
        if per_day:
            have = {r["d"] for r in per_day}
            d0 = datetime.strptime(per_day[0]["d"], "%Y-%m-%d")
            d1 = datetime.strptime(per_day[-1]["d"], "%Y-%m-%d")
            d = d0
            while d <= d1:
                if d.strftime("%Y-%m-%d") not in have:
                    gaps.append(d.strftime("%m-%d"))
                d += timedelta(days=1)

        # 分析: 按粒度聚合最近 12 期
        from .charts import build_periods, agg_gains, agg_published
        from .util import fmt_num as _fn
        periods = build_periods(gran, 12)
        rows = con.videos_with_stats()
        gains, _, est = agg_gains(con, periods, rows)
        counts = agg_published(rows, periods)
        win_start = periods[0][1].strftime("%Y-%m-%d %H:%M:%S")
        win_end = periods[-1][2].strftime("%Y-%m-%d %H:%M:%S")
        growth_rows = filters_mod.apply_filters(
            con.snapshot_report(win_end, win_start),
            filters_mod.args_from_dict({"sort": "growth", "limit": 0},
                                        default_sort="growth"))
        top_growth = growth_rows[:10]
        # 发布时间习惯(小时/星期)
        hour_bins = [0] * 24
        week_bins = [0] * 7
        for r in rows:
            if r.get("created_ts"):
                d = datetime.fromtimestamp(r["created_ts"])
                hour_bins[d.hour] += 1
                week_bins[d.weekday()] += 1
        # 每期账号增量行(供表格/条形)
        acc_rows = {}
        for r in rows:
            acc_rows.setdefault(r["mid"], r.get("account") or str(r["mid"]))
        period_rows = []
        for i, (label, s, e) in enumerate(periods):
            period_rows.append({
                "label": label,
                "snaps": con.con.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE ts>=? AND ts<?",
                    (s.strftime("%Y-%m-%d %H:%M:%S"),
                     e.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()[0],
                "published": sum(c[i] for c in counts.values()),
                "gains": {acc_rows[m]: gains.get(m, [0] * len(periods))[i]
                          for m in acc_rows},
            })
        con.close()
        # 人工分析评论(data/insight.md, 由助手按需手写更新, 非自动生成)
        insight, insight_mtime = None, None
        ipath = os.path.join(os.path.dirname(cfg["storage"]["db_path"]),
                             "insight.md")
        if os.path.exists(ipath):
            try:
                with open(ipath, encoding="utf-8") as f:
                    insight = f.read()
                insight_mtime = datetime.fromtimestamp(
                    os.path.getmtime(ipath)).strftime("%Y-%m-%d %H:%M")
            except OSError:
                pass
        return render_template(
            "data.html", gran=gran, per_day=per_day, by_account=by_account,
            sources=sources, rounds=rounds, total=total,
            total_rounds=total_rounds, first=first, last=last,
            gaps=gaps, periods=periods, period_rows=period_rows,
            gains=gains, estimated=est, top_growth=top_growth,
            hour_bins=hour_bins, week_bins=week_bins, fmt_num=_fn,
            insight=insight, insight_mtime=insight_mtime)

    # ---------- 图表文件 ----------
    @app.route("/charts/<path:fn>")
    def chart_file(fn):
        return send_from_directory(cfg["charts"]["output_dir"], fn)

    # ---------- 运行控制 ----------
    @app.route("/control")
    def control():
        state = _read_state(cfg)
        sched = app.config.get("SCHEDULER")
        running = any(p and p.poll() is None for p in _procs.values()) \
            or (sched.busy() if sched else False)
        con = db()
        cursors = [{"name": (a["name"] or a["mid"]), "mid": a["mid"],
                    "cursor": a["feed_cursor"]}
                   for a in con.con.execute(
                       "SELECT name, mid, feed_cursor FROM accounts").fetchall()]
        con.close()
        schedule = schedmod.load_schedule(cfg)
        return render_template("control.html", state=state, running=running,
                               cursors=cursors,
                               log_tail=_tail(cfg["logging"].get("file"), 80),
                               schedule=schedule,
                               next_runs=[d.strftime("%m-%d %H:%M") for d in
                                          schedmod.next_runs(schedule)],
                               sched_last=(sched.last_run.strftime("%Y-%m-%d %H:%M:%S")
                                           if sched and sched.last_run else None),
                               sched_reason=(sched.last_reason if sched else ""),
                               sched_err=request.args.get("scherr"))

    @app.route("/control/schedule", methods=["POST"])
    def control_schedule():
        ok, err = schedmod.save_schedule(cfg, {
            "times_enabled": request.form.get("times_enabled") == "on",
            "times": request.form.get("times") or "",
            "interval_enabled": request.form.get("interval_enabled") == "on",
            "interval_minutes": request.form.get("interval_minutes") or 0,
            "window_start": request.form.get("window_start") or "",
            "window_end": request.form.get("window_end") or "",
        })
        if not ok:
            from urllib.parse import quote
            return redirect("/control?scherr=" + quote(err))
        return redirect("/control")

    @app.route("/control/run", methods=["POST"])
    def control_run():
        action = request.form.get("action")
        cmds = {"fetch": ["fetch"],
                "chart": ["chart", "--period", "all", "--type", "dashboard"],
                "full": ["fetch", "--full"]}
        if action not in cmds:
            abort(400)
        _spawn(cfg, "chart" if action == "chart" else "fetch", cmds[action])
        return redirect("/control")

    # ---------- 视频报告 ----------
    @app.route("/video")
    def video_page():
        vdir = os.path.join(cfgmod.ROOT, "output", "videos")
        vids = []
        if os.path.isdir(vdir):
            for fn in os.listdir(vdir):
                if not fn.endswith(".mp4"):
                    continue
                p = os.path.join(vdir, fn)
                try:
                    vids.append({
                        "name": fn,
                        "size_mb": os.path.getsize(p) / 1e6,
                        "mtime": datetime.fromtimestamp(
                            os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M"),
                    })
                except OSError:
                    continue
        vids.sort(key=lambda v: v["mtime"], reverse=True)
        running = bool(_procs.get("video") and
                       _procs["video"].poll() is None)
        return render_template("video.html", vids=vids, running=running,
                               err=request.args.get("err"))

    @app.route("/video/run", methods=["POST"])
    def video_run():
        from urllib.parse import quote
        mode = request.form.get("mode", "all")
        try:
            fps = max(10, min(60, int(request.form.get("fps") or 30)))
        except ValueError:
            fps = 30
        cmd = ["video"]
        # 组装 opts JSON(折叠面板逐幕参数), 子进程经 --opts-file 读取
        scenes = {}

        def _num(name, cast=float):
            v = (request.form.get(name) or "").strip()
            if v == "":
                return None
            try:
                return cast(v)
            except ValueError:
                return None

        def _flag(name):
            return request.form.get(name) == "1"

        scene_keys = {"title": ["dur", "title", "subtitle", "title_size", "ring",
                                "blocks"],
                      "overview": ["dur", "game_rows"],
                      "trend": ["dur", "top", "line_width", "axis_pad",
                                "label_width", "dots"],
                      "gains_videos": ["dur", "top", "line_width", "label_width",
                                       "zero_base"],
                      "gains_recent": ["dur", "top", "line_width", "label_width"],
                      "gains_games": ["dur"],
                      "bars": ["dur", "top", "truncate_thr", "bar_h"],
                      "end": ["dur", "text"]}
        bools = {"ring", "blocks", "dots", "zero_base"}
        texts = {"title", "subtitle", "text"}
        for sc, keys in scene_keys.items():
            conf = {}
            for k in keys:
                if k in bools:
                    conf[k] = _flag(f"sc_{sc}_{k}")
                    continue
                v = (request.form.get(f"sc_{sc}_{k}") or "").strip()
                if v == "":
                    continue
                conf[k] = v if k in texts else _num(f"sc_{sc}_{k}")
            if conf:
                scenes[sc] = conf
        opts = {"sweep_frac": _num("sweep_frac"), "scenes": scenes,
                "fps": fps, "style": request.form.get("style") or "fluid"}
        mode = request.form.get("mode", "all")
        opts["mode"] = mode
        if mode == "days":
            opts["days"] = _num("days", int) or 7
        elif mode == "range":
            opts["from"] = request.form.get("from") or ""
            opts["to"] = request.form.get("to") or ""
        data_dir = os.path.dirname(cfg["storage"]["db_path"])
        os.makedirs(data_dir, exist_ok=True)
        opts_path = os.path.join(data_dir, "gui_video_opts.json")
        with open(opts_path, "w", encoding="utf-8") as f:
            json.dump(opts, f, ensure_ascii=False)
        cmd += ["--opts-file", opts_path]
        if mode == "days":
            try:
                days = max(1, min(365, int(request.form.get("days") or 7)))
            except ValueError:
                days = 7
            cmd += ["--days", str(days)]
        elif mode == "range":
            frm = _parse_time(request.form.get("from") or "")
            to = _parse_time(request.form.get("to") or "")
            if not frm or not to or frm >= to:
                return redirect("/video?err=" + quote("时间范围无效: 起点需早于终点"))
            cmd += ["--from", frm, "--to", to]
        _spawn(cfg, "video", cmd)
        return redirect("/video")

    @app.route("/video-files/<path:fn>")
    def video_file(fn):
        return send_from_directory(
            os.path.join(cfgmod.ROOT, "output", "videos"), fn, conditional=True)

    return app


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def run_server(cfg, port=8322, open_browser=True):
    app = create_app(cfg)
    sched = schedmod.Scheduler(cfg, MAIN_PY)
    app.config["SCHEDULER"] = sched
    threading.Thread(target=sched.run_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"Web GUI 已启动: {url}  (Ctrl+C 停止)")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
