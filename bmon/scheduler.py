"""定时采集调度器: 支持每日固定时间点 + 时段内按间隔采集.

计划保存在 data/schedule.json, 可在 Web GUI "运行控制" 页随时修改,
调度循环每 20 秒重读一次文件, 修改后无需重启即生效.
采集以子进程方式调用 main.py fetch, 与手动按钮行为完全一致.
"""
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

log = logging.getLogger("bmon.scheduler")

MAIN_PY = None  # run_scheduler 时由调用方注入项目根 main.py 路径

DEFAULT_SCHEDULE = {
    "times_enabled": True,
    "times": ["21:30"],        # 方式一: 每日固定采集时间点 HH:MM (可多个, 逗号分隔)
    "interval_enabled": False,
    "interval_minutes": 60,    # 方式二: 时段内每 N 分钟采集一次
    "window_start": "08:00",
    "window_end": "23:59",
}
MIN_GAP_SECONDS = 10 * 60      # 两次采集最小间隔, 防止重叠触发


def _data_dir(cfg):
    return os.path.dirname(os.path.abspath(cfg["storage"]["db_path"]))


def schedule_path(cfg):
    return os.path.join(_data_dir(cfg), "schedule.json")


def _state_path(cfg):
    return os.path.join(_data_dir(cfg), "schedule_state.json")


def load_schedule(cfg):
    sch = dict(DEFAULT_SCHEDULE)
    try:
        with open(schedule_path(cfg), encoding="utf-8") as f:
            sch.update(json.load(f))
    except Exception:
        pass
    # 兼容旧格式: {enabled, interval_minutes>0 即视为开启间隔采集}
    if "enabled" in sch and "times_enabled" not in sch:
        sch["times_enabled"] = bool(sch.get("enabled"))
    if "interval_enabled" not in sch:
        sch["interval_enabled"] = int(sch.get("interval_minutes") or 0) > 0
    sch["times"] = [t for t in (sch.get("times") or []) if _parse_hhmm(t)]
    return sch


def save_schedule(cfg, data):
    """校验并保存计划; 返回 (ok, 错误信息). 两种方式各自独立启用."""
    times_enabled = bool(data.get("times_enabled"))
    interval_enabled = bool(data.get("interval_enabled"))
    times = []
    if times_enabled:
        for t in str(data.get("times") or "").replace("，", ",").split(","):
            t = t.strip()
            if not t:
                continue
            if not _parse_hhmm(t):
                return False, f"时间点格式错误: {t} (应为 HH:MM, 如 21:30; 多个用英文逗号隔开)"
            times.append(datetime.strptime(t, "%H:%M").strftime("%H:%M"))
        if not times:
            return False, "已启用每日时间点采集, 请至少填写一个时间点 (如 21:30)"
    try:
        interval = max(0, int(data.get("interval_minutes") or 0))
    except (TypeError, ValueError):
        return False, "采集间隔应为非负整数(分钟)"
    win_s = str(data.get("window_start") or "00:00").strip() or "00:00"
    win_e = str(data.get("window_end") or "23:59").strip() or "23:59"
    if interval_enabled:
        for w in (win_s, win_e):
            if not _parse_hhmm(w):
                return False, f"时段格式错误: {w} (应为 HH:MM)"
        if interval <= 0:
            return False, "已启用间隔采集, 间隔分钟数需大于 0"
        if win_s >= win_e:
            return False, "间隔采集的时段起点必须早于终点"
    sch = {"times_enabled": times_enabled, "times": sorted(set(times)),
           "interval_enabled": interval_enabled, "interval_minutes": interval,
           "window_start": win_s, "window_end": win_e}
    os.makedirs(_data_dir(cfg), exist_ok=True)
    with open(schedule_path(cfg), "w", encoding="utf-8") as f:
        json.dump(sch, f, ensure_ascii=False, indent=2)
    log.info("采集计划已更新: %s", sch)
    return True, None


def _parse_hhmm(s):
    try:
        return datetime.strptime(str(s).strip(), "%H:%M")
    except (TypeError, ValueError):
        return None


def _load_state(cfg):
    try:
        with open(_state_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_run": None, "fired": {}}


def _save_state(cfg, state):
    try:
        os.makedirs(_data_dir(cfg), exist_ok=True)
        with open(_state_path(cfg), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("写入调度状态失败")


def next_runs(sch, now=None, count=4):
    """预测接下来 count 次触发时间(展示用)."""
    now = now or datetime.now()
    out = []
    base = now.replace(second=0, microsecond=0)
    # 固定时间点: 今天剩余 + 明天
    if sch.get("times_enabled", True):
        for day_off in (0, 1):
            day = (base + timedelta(days=day_off)).replace(hour=0, minute=0)
            for t in sch.get("times", []):
                hm = _parse_hhmm(t)
                cand = day.replace(hour=hm.hour, minute=hm.minute)
                if cand > now:
                    out.append(cand)
    # 间隔模式: 从窗口起点逐间隔推演(最多2天)
    interval = int(sch.get("interval_minutes") or 0)
    if interval > 0 and sch.get("interval_enabled"):
        ws, we = _parse_hhmm(sch.get("window_start", "00:00")), \
                 _parse_hhmm(sch.get("window_end", "23:59"))
        for day_off in (0, 1):
            day = (base + timedelta(days=day_off)).replace(hour=0, minute=0)
            t = day.replace(hour=ws.hour, minute=ws.minute)
            end = day.replace(hour=we.hour, minute=we.minute)
            while t <= end:
                if t > now:
                    out.append(t)
                t += timedelta(minutes=interval)
    out.sort()
    seen, dedup = set(), []
    for d in out:
        k = d.strftime("%Y-%m-%d %H:%M")
        if k not in seen:
            seen.add(k)
            dedup.append(d)
    return dedup[:count]


class Scheduler:
    """调度主循环; 可在独立进程(main.py scheduler)或 Web GUI 后台线程中运行."""

    def __init__(self, cfg, main_py):
        self.cfg = cfg
        self.main_py = main_py
        self.proc = None
        self.last_run = None       # datetime, 最近一次启动的采集
        self.last_reason = ""

    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def tick(self, now=None):
        now = now or datetime.now()
        if self.busy():
            return
        if self.last_run and (now - self.last_run).total_seconds() < MIN_GAP_SECONDS:
            return
        sch = load_schedule(self.cfg)
        state = _load_state(self.cfg)
        reason = None
        hm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        fired_today = state.get("fired", {}).get(today, [])
        if sch.get("times_enabled", True):
            for t in sch.get("times", []):
                if t == hm and t not in fired_today:
                    reason = f"定时时间点 {t}"
                    fired_today.append(t)
                    break
        if reason is None and sch.get("interval_enabled") \
                and int(sch.get("interval_minutes") or 0) > 0:
            ws = _parse_hhmm(sch.get("window_start", "00:00"))
            we = _parse_hhmm(sch.get("window_end", "23:59"))
            cur = now.hour * 60 + now.minute
            in_win = ws.hour * 60 + ws.minute <= cur <= we.hour * 60 + we.minute
            last = state.get("last_run")
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S") if last else None
            if in_win and (last_dt is None or
                           (now - last_dt).total_seconds() >=
                           int(sch["interval_minutes"]) * 60):
                reason = f"时段内每 {sch['interval_minutes']} 分钟"
        if reason is None:
            return
        log.info("调度器触发采集: %s", reason)
        os.makedirs(_data_dir(self.cfg), exist_ok=True)
        logf = open(os.path.join(_data_dir(self.cfg), "scheduled.log"), "ab")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, self.main_py, "fetch"],
                cwd=os.path.dirname(os.path.abspath(self.main_py)),
                stdout=logf, stderr=subprocess.STDOUT)
        except Exception:
            log.exception("启动采集子进程失败")
            return
        self.last_run = now
        self.last_reason = reason
        state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        fired = {today: fired_today}          # 仅保留当日记录
        state["fired"] = fired
        _save_state(self.cfg, state)

    def run_forever(self):
        log.info("采集调度器已启动 (计划: %s)", load_schedule(self.cfg))
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("调度器异常")
            time.sleep(20)
