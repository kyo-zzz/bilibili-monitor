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
from .storage import Database
from .util import fmt_num

log = logging.getLogger("bmon.webui")

MAIN_PY = os.path.join(cfgmod.ROOT, "main.py")
_procs = {"fetch": None, "chart": None}


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
        return render_template(
            "index.html", per=list(per.values()), state=state,
            chart_files=chart_files, videos_total=len(rows),
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
        sel = request.args.get("bvid") or (top[0]["bvid"] if top else "")
        sel2 = request.args.get("bvid2") or ""
        info = next((r for r in all_rows if r.get("bvid") == sel), None)
        info2 = next((r for r in all_rows if r.get("bvid") == sel2), None)
        return render_template("trend.html", top=top, sel=sel, sel2=sel2,
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

    # ---------- 图表文件 ----------
    @app.route("/charts/<path:fn>")
    def chart_file(fn):
        return send_from_directory(cfg["charts"]["output_dir"], fn)

    # ---------- 运行控制 ----------
    @app.route("/control")
    def control():
        state = _read_state(cfg)
        running = any(p and p.poll() is None for p in _procs.values())
        con = db()
        cursors = [{"name": (a["name"] or a["mid"]), "mid": a["mid"],
                    "cursor": a["feed_cursor"]}
                   for a in con.con.execute(
                       "SELECT name, mid, feed_cursor FROM accounts").fetchall()]
        con.close()
        return render_template("control.html", state=state, running=running,
                               cursors=cursors,
                               log_tail=_tail(cfg["logging"].get("file"), 40))

    @app.route("/control/run", methods=["POST"])
    def control_run():
        action = request.form.get("action")
        cmds = {"fetch": ["fetch"],
                "chart": ["chart", "--period", "both", "--type", "dashboard"],
                "full": ["fetch", "--full"]}
        if action not in cmds:
            abort(400)
        key = "chart" if action == "chart" else "fetch"
        p = _procs.get(key)
        if p and p.poll() is None:
            return redirect("/control")
        data_dir = os.path.join(os.path.dirname(cfg["storage"]["db_path"]))
        os.makedirs(data_dir, exist_ok=True)
        logf = open(os.path.join(data_dir, "gui_runs.log"), "ab")
        _procs[key] = subprocess.Popen(
            [sys.executable, MAIN_PY] + cmds[action], cwd=cfgmod.ROOT,
            stdout=logf, stderr=subprocess.STDOUT)
        log.info("GUI 触发子进程: %s", cmds[action])
        return redirect("/control")

    return app


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def run_server(cfg, port=8322, open_browser=True):
    app = create_app(cfg)
    url = f"http://127.0.0.1:{port}"
    print(f"Web GUI 已启动: {url}  (Ctrl+C 停止)")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
