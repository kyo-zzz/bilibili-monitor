#!/usr/bin/env python3
"""B站官号视频播放量自动监测系统 - 命令行入口.

常用命令:
  python main.py init                          # 生成默认配置 config.yaml
  python main.py find --keyword 绝区零          # 联网搜索用户, 查 mid 用于配置
  python main.py accounts                      # 核验配置中的账号(联网)
  python main.py fetch [--full]                # 执行一轮采集
  python main.py run [--interval-minutes 30]   # 持续自动监测 + 自动出图
  python main.py chart --period weekly --type dashboard
  python main.py list --keyword 前瞻 --sort views --limit 20 [--csv out.csv]
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bmon import __version__, config as cfgmod
from bmon.util import fmt_num


def _utf8_stdio():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def setup_logging(cfg):
    level = getattr(logging, str(cfg["logging"].get("level", "INFO")).upper(),
                    logging.INFO)
    logfile = cfg["logging"].get("file")
    handlers = []
    if sys.stdout is not None:  # 计划任务/pythonw 环境下可能无标准流
        handlers.append(logging.StreamHandler(sys.stdout))
    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", handlers=handlers)


def cmd_init(args):
    if os.path.exists(args.config):
        print(f"配置已存在: {args.config} (如需重置请先删除)")
        return
    with open(args.config, "w", encoding="utf-8") as f:
        f.write(cfgmod.CONFIG_TEMPLATE)
    print(f"已生成默认配置: {args.config}")
    print("建议先运行 `python main.py accounts` 核验账号, 再 `python main.py fetch` 采集。")


def cmd_find(args):
    from bmon.api import BiliApi
    api = BiliApi({})
    print(f"搜索B站用户: {args.keyword}\n")
    users = api.search_users(args.keyword, pages=args.pages)
    if not users:
        print("未找到用户(若触发风控请稍后重试)")
        return
    print(f"{'mid':<12}{'粉丝':>10}  {'名称':<18}认证")
    print("-" * 72)
    for u in users[:25]:
        print(f"{str(u['mid']):<12}{fmt_num(u['fans']):>10}  "
              f"{u['uname']:<18}{u['official']}")
    print("\n把选定的 mid 填入 config.yaml 的 accounts 段即可。")


def cmd_accounts(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon.api import BiliApi
    from bmon.storage import Database
    api = BiliApi(cfg["monitor"])
    db = Database(cfg["storage"]["db_path"])
    accs = cfg.get("accounts") or []
    if not accs:
        print("config.yaml 中未配置账号")
        return
    print(f"{'启用':<4} {'mid':<12}{'配置名':<14}{'B站昵称':<18}{'粉丝':>9}{'库内视频':>8}")
    print("-" * 72)
    for a in accs:
        mid = a.get("mid")
        enabled = a.get("enabled", True)
        if not mid:
            print(f"{'✖':<4} {'(缺mid)':<12}{str(a.get('name', '')):<14}(请用 find 查询)")
            continue
        try:
            card = api.account_card(mid)
            name = (card.get("card") or {}).get("name")
            fans = card.get("follower")
            cnt = db.video_count(mid)
            print(f"{'✔' if enabled else '·':<4} {mid:<12}{str(a.get('name', '')):<14}"
                  f"{str(name):<18}{fmt_num(fans):>9}{cnt:>8}")
        except Exception as e:
            print(f"{'✖':<4} {mid:<12}{str(a.get('name', '')):<14}获取失败: {e}")
    db.close()


def cmd_fetch(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon.monitor import Monitor
    Monitor(cfg).run_once(full=args.full)


def cmd_run(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon.monitor import Monitor
    try:
        Monitor(cfg).run_loop(interval_minutes=args.interval_minutes,
                              max_cycles=args.max_cycles)
    except KeyboardInterrupt:
        print("\n已手动停止监测。")


def cmd_chart(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    if args.periods_back:
        cfg["charts"]["periods_back"] = args.periods_back
    from bmon import charts, filters
    from bmon.storage import Database
    db = Database(cfg["storage"]["db_path"])
    charts.setup_font(cfg["charts"].get("font"))
    rows = filters.apply_filters(db.videos_with_stats(), args)
    if args.period == "all":
        kinds = ["daily", "weekly", "monthly"]
    elif args.period == "both":
        kinds = ["weekly", "monthly"]
    else:
        kinds = [args.period]
    for kind in kinds:
        if args.type == "dashboard":
            path = charts.make_dashboard(db, cfg, kind, rows)
        else:
            path = charts.make_single(db, cfg, kind, args.type, rows)
        print("已生成:", path)
    print("索引页:", charts.write_index(cfg))
    db.close()


def cmd_list(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon import filters
    from bmon.storage import Database
    db = Database(cfg["storage"]["db_path"])
    all_rows = db.videos_with_stats()
    rows = filters.apply_filters(all_rows, args)
    if not rows:
        print("没有符合条件的视频(可先运行 fetch 采集数据)")
        return
    filters.print_table(rows, total=len(rows))
    if args.csv:
        filters.write_csv(rows, args.csv)
    db.close()


def cmd_state(args):
    import json
    cfg = cfgmod.load_config(args.config)
    state_path = os.path.join(os.path.dirname(cfg["storage"]["db_path"]), "state.json")
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
    else:
        print("暂无运行状态(尚未采集过)")


def _parse_time(s, name):
    import datetime as dt
    s = str(s).strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, f)
            if f == "%Y-%m-%d":
                pass
            return d.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise SystemExit(f"{name} 时间格式错误: {s} (应为 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD')")


def cmd_snapshot(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon import filters
    from bmon.storage import Database
    import datetime as dt
    db = Database(cfg["storage"]["db_path"])
    if args.at:
        ts_to = _parse_time(args.at, "--at")
        ts_from = None
    elif args.frm and args.to:
        ts_from = _parse_time(args.frm, "--from")
        ts_to = _parse_time(args.to, "--to")
        if ts_from >= ts_to:
            raise SystemExit("--from 必须早于 --to")
    else:
        raise SystemExit("需要 --at <时刻> 或 --from <起> --to <止>")
    rows = db.snapshot_report(ts_to, ts_from)
    rows = filters.apply_filters(rows, args)
    if not rows:
        print("该时间点/时段没有匹配的数据(快照从开始监测起积累)")
        return
    mode = "时段 %s ~ %s" % (ts_from, ts_to) if ts_from else "时刻 %s" % ts_to
    print(f"快照查询 · {mode}\n")
    filters.print_snapshot_table(rows, ranged=bool(ts_from))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            f.write(filters.csv_content(rows, fields=filters.SNAP_CSV_FIELDS))
        print(f"已导出 CSV: {args.csv}")
    db.close()


def cmd_gui(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon.webui import run_server
    run_server(cfg, port=args.port, open_browser=not args.no_browser)


def cmd_scheduler(args):
    """常驻定时采集(不启动Web界面); 计划同样读 data/schedule.json."""
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon.scheduler import Scheduler
    try:
        Scheduler(cfg, os.path.abspath(__file__)).run_forever()
    except KeyboardInterrupt:
        print("\n已停止定时采集调度。")


def cmd_video(args):
    cfg = cfgmod.load_config(args.config)
    setup_logging(cfg)
    from bmon.storage import Database
    from bmon import video
    db = Database(cfg["storage"]["db_path"])
    row = db.con.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
    if not row[0]:
        raise SystemExit("尚无快照数据, 请先执行采集 (fetch)")
    if args.frm and args.to:
        ts_from, ts_to = _parse_time(args.frm, "--from"), _parse_time(args.to, "--to")
    else:
        import datetime as dt
        ts_to = row[1]
        if args.days and args.days > 0:
            lo = (dt.datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
                  - dt.timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
            ts_from = max(row[0], lo)
        else:
            ts_from = row[0]                       # 默认: 全部快照历史
    if ts_from >= ts_to:
        raise SystemExit("--from 必须早于 --to")
    path = video.make_video(db, cfg, ts_from, ts_to, fps=args.fps)
    print("已生成视频:", path)
    db.close()


def build_parser():
    p = argparse.ArgumentParser(
        prog="main.py",
        description="B站官号视频播放量自动监测系统 "
                    "(原神 / 崩坏:星穹铁道 / 绝区零 等官号, 账号可自定义)")
    p.add_argument("--config", default=os.path.join(cfgmod.ROOT, "config.yaml"),
                   help="配置文件路径 (默认: 项目根目录 config.yaml)")
    p.add_argument("--version", action="version", version=f"bmon {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="生成默认配置文件").set_defaults(func=cmd_init)

    pf = sub.add_parser("find", help="搜索B站用户, 查询 mid")
    pf.add_argument("--keyword", required=True, help="用户名关键词")
    pf.add_argument("--pages", type=int, default=1)
    pf.set_defaults(func=cmd_find)

    sub.add_parser("accounts", help="核验配置中的账号(联网)").set_defaults(func=cmd_accounts)

    pfe = sub.add_parser("fetch", help="执行一轮采集")
    pfe.add_argument("--full", action="store_true",
                     help="强制全量翻页回填(忽略增量优化)")
    pfe.set_defaults(func=cmd_fetch)

    pr = sub.add_parser("run", help="持续自动监测(循环采集+自动图表)")
    pr.add_argument("--interval-minutes", type=float, default=None,
                    help="覆盖配置中的监测间隔(分钟, 支持小数)")
    pr.add_argument("--max-cycles", type=int, default=None,
                    help="最多运行N轮后退出(调试用)")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("chart", help="生成图表(支持筛选参数)")
    pc.add_argument("--period", choices=["daily", "weekly", "monthly", "both", "all"],
                    default="both")
    pc.add_argument("--type", choices=["dashboard", "published", "gained", "top"],
                    default="dashboard")
    pc.add_argument("--periods-back", type=int, default=0, help="覆盖展示周期数")
    from bmon import filters as fmod
    fmod.add_filter_args(pc)
    pc.set_defaults(func=cmd_chart)

    pl = sub.add_parser("list", help="按条件筛选视频")
    fmod.add_filter_args(pl)
    pl.add_argument("--csv", help="导出CSV路径 (如 out.csv)")
    pl.set_defaults(func=cmd_list)

    sub.add_parser("state", help="查看运行状态").set_defaults(func=cmd_state)

    ps = sub.add_parser("snapshot", help="查询任意时刻/时段的播放量快照")
    ps.add_argument("--at", help="查询该时刻数据, 如 '2026-08-16 12:00'")
    ps.add_argument("--from", dest="frm", help="时段起点(与 --to 搭配)")
    ps.add_argument("--to", help="时段终点")
    ps.add_argument("--csv", help="导出CSV路径")
    fmod.add_filter_args(ps)
    ps.set_defaults(sort="growth")
    ps.set_defaults(func=cmd_snapshot)

    pg = sub.add_parser("gui", help="启动本地 Web GUI(数据查看+采集控制+定时采集配置)")
    pg.add_argument("--port", type=int, default=8322)
    pg.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    pg.set_defaults(func=cmd_gui)

    sub.add_parser("scheduler",
                   help="常驻定时采集调度(计划可在 Web GUI 运行控制页配置)").set_defaults(
        func=cmd_scheduler)

    pv = sub.add_parser("video", help="生成指定时期数据变化可视化视频(1080p MP4)")
    pv.add_argument("--days", type=int, default=0,
                    help="仅取最近N天快照(0=全部快照历史)")
    pv.add_argument("--from", dest="frm", help="时段起点 YYYY-MM-DD [HH:MM]")
    pv.add_argument("--to", help="时段终点")
    pv.add_argument("--fps", type=int, default=30)
    pv.set_defaults(func=cmd_video)
    return p


def main():
    _utf8_stdio()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
