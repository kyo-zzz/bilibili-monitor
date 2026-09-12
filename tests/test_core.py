"""核心纯逻辑单元测试: 筛选/快照语义/图表折行/视频参数/调度校验/互斥锁.

运行: python -m pytest tests -q
不涉及任何网络请求.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bmon import filters, lock  # noqa: E402
from bmon.storage import Database  # noqa: E402


# ---------- fixtures ----------
@pytest.fixture()
def db(tmp_path):
    con = Database(str(tmp_path / "t.db"))
    con.upsert_account(111, name="游戏A")
    con.upsert_account(222, name="游戏B")
    con.upsert_videos([
        {"bvid": "BV1", "mid": 111, "title": "视频一", "created_ts": 1000,
         "created_at": "2026-01-01 00:16:40", "length_text": "1:00",
         "duration": 60, "pic": None, "dyn_id": None},
        {"bvid": "BV2", "mid": 222, "title": "视频二", "created_ts": 2000,
         "created_at": "2026-01-01 00:33:20", "length_text": "2:00",
         "duration": 120, "pic": None, "dyn_id": None},
    ])
    yield con
    con.close()


@pytest.fixture()
def snaps(db):
    def add(bvid, ts, view):
        db.con.execute(
            "INSERT OR IGNORE INTO snapshots(bvid,ts,view,source) VALUES(?,?,?,'detail')",
            (bvid, ts, view))
    add("BV1", "2026-08-16 10:00:00", 1000)
    add("BV1", "2026-08-16 20:00:00", 1500)
    add("BV1", "2026-08-17 10:00:00", 1800)
    add("BV2", "2026-08-16 20:00:00", 500)
    db.con.commit()
    return db


# ---------- storage / 快照语义 ----------
def test_snapshot_report_single_time(snaps):
    rows = snaps.snapshot_report("2026-08-16 21:00:00")
    by = {r["bvid"]: r for r in rows}
    assert by["BV1"]["latest_view"] == 1500      # <= 21:00 的最近快照
    assert by["BV1"]["latest_ts"] == "2026-08-16 20:00:00"
    assert by["BV2"]["latest_view"] == 500
    assert "BV3" not in by                       # 该时刻之后才有快照的不出现


def test_snapshot_report_range_growth(snaps):
    rows = snaps.snapshot_report("2026-08-17 12:00:00", "2026-08-16 12:00:00")
    by = {r["bvid"]: r for r in rows}
    assert by["BV1"]["first_view"] == 1000       # 期初 = <=from 最近
    assert by["BV1"]["latest_view"] == 1800
    assert filters.growth_of(by["BV1"]) == 800


def test_missing_meta_roundtrip(db):
    assert set(db.videos_missing_meta(111)) == {"BV1"}
    db.mark_meta_done("BV1")
    assert db.videos_missing_meta(111) == []


# ---------- filters ----------
def _row(bvid="BV1", title="标题", view=100, ts=1700000000, mid=111, dur=60):
    return {"bvid": bvid, "mid": mid, "account": "游戏A", "title": title,
            "created_ts": ts, "created_at": "2026-01-01 00:00:00",
            "duration": dur, "tname": None, "latest_view": view,
            "latest_likes": None, "first_view": view - 10, "latest_ts": None}


def test_apply_filters_keyword_and_min_views():
    rows = [_row("BV1", "原神PV", 500), _row("BV2", "PV合集", 5000)]
    args = filters.args_from_dict({"keyword": "pv", "min_views": "1000"})
    out = filters.apply_filters(rows, args)
    assert [r["bvid"] for r in out] == ["BV2"]


def test_apply_filters_sort_growth():
    rows = [_row("BV1", "a", 100), _row("BV2", "b", 300), _row("BV3", "c", 200)]
    for r in rows:
        r["first_view"] = 50
    args = filters.args_from_dict({"sort": "growth"})
    out = filters.apply_filters(rows, args)
    assert [r["bvid"] for r in out] == ["BV2", "BV3", "BV1"]


def test_args_from_dict_limit_default():
    ns = filters.args_from_dict({})
    assert ns.sort == "views" and ns.limit == 30
    ns2 = filters.args_from_dict({"limit": "0"})
    assert ns2.limit == 0


# ---------- charts ----------
def test_build_periods_kinds():
    from bmon.charts import build_periods
    for kind in ("daily", "weekly", "monthly"):
        ps = build_periods(kind, 12)
        assert len(ps) == 12
        assert all(ps[i][1] < ps[i + 1][1] for i in range(11))  # 时间升序
        assert all(p[1] < p[2] for p in ps)                       # 起<止


def test_wrap_title_never_loses_text():
    from bmon.charts import _wrap_title
    s = "《崩坏：星穹铁道》4.5版本「挥掷千星的筹码」前瞻特别节目"
    out = _wrap_title(s)
    assert "…" not in out
    assert out.replace("\n", "") == s               # 折行不丢字


# ---------- video ----------
def test_video_wrap2_and_bar_axis():
    from bmon.video import _wrap2, _bar_axis, _interp_value
    s = "很长的视频标题" * 10
    out = _wrap2(s, 20)
    assert out.replace("\n", "") == s
    tops = [{"start": 4000000, "end": 5000000}, {"start": 4100000, "end": 4600000}]
    lo, hi = _bar_axis(tops)
    assert lo > 0 and hi >= 5000000                  # 差异小时抬升轴起点
    tops2 = [{"start": 100, "end": 900}, {"start": 50, "end": 200}]
    assert _bar_axis(tops2)[0] == 0                  # 差异大时从0起
    from datetime import datetime
    pts = [(datetime(2026, 8, 16, 10), 10), (datetime(2026, 8, 16, 11), 30)]
    assert _interp_value(pts, datetime(2026, 8, 16, 10, 30)) == 20


# ---------- scheduler ----------
def _cfg(tmp_path):
    return {"storage": {"db_path": str(tmp_path / "data" / "monitor.db")}}


def test_save_schedule_validation(tmp_path):
    from bmon import scheduler
    cfg = _cfg(tmp_path)
    ok, err = scheduler.save_schedule(cfg, {"times_enabled": True, "times": "9:99"})
    assert not ok and "格式错误" in err
    ok, err = scheduler.save_schedule(
        cfg, {"times_enabled": True, "times": "21:30"})
    assert ok and err is None
    ok, err = scheduler.save_schedule(
        cfg, {"interval_enabled": True, "interval_minutes": 0})
    assert not ok
    ok, err = scheduler.save_schedule(
        cfg, {"interval_enabled": True, "interval_minutes": 30,
              "window_start": "22:00", "window_end": "08:00"})
    assert not ok and "早于" in err


def test_next_runs_ordered(tmp_path):
    from bmon import scheduler
    cfg = _cfg(tmp_path)
    scheduler.save_schedule(cfg, {
        "times_enabled": True, "times": "08:00, 21:30",
        "interval_enabled": True, "interval_minutes": 120,
        "window_start": "08:00", "window_end": "22:00"})
    runs = scheduler.next_runs(scheduler.load_schedule(cfg), count=4)
    assert len(runs) == 4
    assert runs == sorted(runs)


# ---------- 全量扫描调度 ----------
def test_full_sweep_due():
    import datetime as dt
    from bmon.monitor import full_sweep_due
    now = dt.datetime(2026, 8, 30, 12, 0)
    assert full_sweep_due(None, now, 3, 45) is True          # 从未全量 → 立即
    assert full_sweep_due("2026-08-30 06:00:00", now, 3, 45) is False
    old = (now - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    assert full_sweep_due(old, now, 3, 45) is True            # 满 3 天
    assert full_sweep_due(old, now, 7, 45) is False           # 未满 7 天
    assert full_sweep_due(None, now, 0, 45) is False          # 功能关闭
    assert full_sweep_due(None, now, 3, 0) is False           # active=0 本就每轮全量
    assert full_sweep_due("bad", now, 3, 45) is True          # 坏数据视为到期


# ---------- 视频时间轴: 起始日0点 + 末端=末时间(右侧无空白) ----------
def test_time_axis_day_normalized():
    import datetime as dt
    from bmon.video_fluid import _time_axis, _day_grid, _interp_x
    d0, nd = _time_axis({"ts_from": "2026-09-04 21:30:00",
                         "ts_to": "2026-09-11 21:30:00"})
    assert d0 == _dt(2026, 9, 4).date()                       # 起始日
    assert nd == 8                                            # 9/4..9/11 每天一格
    pos, labeled = _day_grid(nd)
    assert pos == list(range(8)) and labeled == pos           # 均匀且全标(末端必标)
    # 9/4→9/6 恰两格; 9/10→9/11 恰一格(末端就是 09-11, 无多余半格)
    assert labeled[2] - labeled[0] == 2 and labeled[7] - labeled[6] == 1
    # 日索引插值: 端外持有, 线性插值
    pts = [(0, 100), (1, 200), (7, 400)]
    assert _interp_x(pts, 0.5) == 150 and _interp_x(pts, 9) == 400


def test_recent_filter_caps_to_top_n():
    """范围内视频多于 top_n 时必须截断(竞跑 Top38 bug 回归测试)."""
    from bmon.video_fluid import _recent_filter
    t0, t1 = _dt(2026, 9, 4), _dt(2026, 9, 11)
    items = [{"bvid": f"B{i}", "created_ts": _dt(2026, 9, 5).timestamp() + i,
              "end": 1000 - i, "start": 0} for i in range(38)]
    out, _, fb = _recent_filter(items, t0, t1, top_n=10, mult=2.0)
    assert len(out) == 10 and fb is False
    assert out[0]["end"] == 1000 and out[-1]["end"] == 991   # 按期末播放降序取前十


def test_recent_filter_fallback():
    from bmon.video_fluid import _recent_filter
    t0, t1 = _dt(2026, 9, 4, 21, 30), _dt(2026, 9, 11, 21, 30)
    items = [
        {"bvid": "A", "created_ts": _dt(2026, 9, 10).timestamp(),
         "end": 500, "start": 0},
        {"bvid": "B", "created_ts": _dt(2026, 8, 20).timestamp(),
         "end": 900, "start": 0},
        {"bvid": "C", "created_ts": _dt(2026, 8, 1).timestamp(),
         "end": 700, "start": 0},
        {"bvid": "D", "created_ts": _dt(2026, 7, 1).timestamp(),
         "end": 600, "start": 0},
    ]
    out, eff, fb = _recent_filter(items, t0, t1, top_n=3)
    assert [o["bvid"] for o in out] == ["B", "C", "A"]        # 按期末播放降序
    assert fb is True and eff == _dt(2026, 8, 1).timestamp()
    # 范围内足够 → 不触发兜底
    out2, _, fb2 = _recent_filter(items[:2] + [
        {"bvid": "E", "created_ts": _dt(2026, 9, 9).timestamp(),
         "end": 100, "start": 0}], t0, t1, top_n=2)
    assert fb2 is False and len(out2) == 2


# ---------- lock ----------
def test_lock_mutual_exclusion(tmp_path):
    p = str(tmp_path / "fetch.lock")
    assert lock.acquire(p)
    assert not lock.acquire(p)                        # 二次获取失败
    lock.release(p)
    assert lock.acquire(p)                            # 释放后可重新获取
    lock.release(p)


def test_lock_ttl_expiry(tmp_path):
    p = str(tmp_path / "fetch.lock")
    assert lock.acquire(p)
    lock.release(p)
    # 手工构造一把过期锁
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"pid": 1, "ts": %f}' % (time.time() - 99999))
    assert lock.acquire(p, ttl=60)                    # 过期可抢占
    lock.release(p)


# ---------- 展示插值估算(仅展示, 不入快照库) ----------
def _dt(*a):
    import datetime as _dt
    return _dt.datetime(*a)


def test_estimate_at_interpolation_and_extrapolation():
    from bmon.interp import estimate_at
    pts = [(_dt(2026, 8, 16, 10), 1000), (_dt(2026, 8, 16, 20), 1500)]
    # 区间内: 线性插值
    v, est = estimate_at(pts, _dt(2026, 8, 16, 15))
    assert v == 1250 and est is False
    # 末段外推: 20:00 后按斜率 50/h
    v, est = estimate_at(pts, _dt(2026, 8, 16, 22))
    assert v == 1600 and est is True
    # 外推斜率为负 → 钳位为 0(持有末值)
    pts2 = [(_dt(2026, 8, 16, 10), 2000), (_dt(2026, 8, 16, 20), 1500)]
    v, est = estimate_at(pts2, _dt(2026, 8, 16, 22))
    assert v == 1500 and est is True
    # 超过 7 天视界 → 持有末值
    v, est = estimate_at(pts, _dt(2026, 8, 30), max_horizon=__import__("datetime").timedelta(days=7))
    assert v == 1500 and est is True
    # 早于首点 → 持有首值
    v, est = estimate_at(pts, _dt(2026, 8, 16, 8))
    assert v == 1000 and est is False


def test_agg_gains_interpolates_missing_tail(snaps):
    """期末无快照的时段由插值/外推估算, 且计入 estimated 标记."""
    from bmon.charts import agg_gains
    periods = [("P1", _dt(2026, 8, 16, 0), _dt(2026, 8, 17, 0)),
               ("P2", _dt(2026, 8, 17, 0), _dt(2026, 8, 18, 0))]
    rows = snaps.videos_with_stats()
    gains, top, est = agg_gains(snaps, periods, rows, interpolate=True,
                                now=_dt(2026, 8, 17, 20))
    # BV1: P1 基线=期前真实快照1000 → 期末17/00:00 插值≈1586 → +586;
    #      P2 基线=期前真实快照1500(估算值不入序列) → 期末17/20:00 外推≈2014 → +514
    assert gains[111][0] == round(1500 + 300 * (4 / 14) - 1000)
    assert gains[111][1] == round(1800 + 300 * (10 / 14) - 1500)
    assert est is True                                  # P2 期末为外推
    assert any(g > 0 for g, _ in top)
    # 关闭插值 → 旧语义: 期末=期内最后真实快照
    gains2, _, est2 = agg_gains(snaps, periods, rows, interpolate=False,
                                now=_dt(2026, 8, 17, 20))
    assert gains2[111][0] == 500                        # P1 期末=期内最后快照1500
    assert gains2[111][1] == 300                        # P2: 1800 − 基线1500
    assert est2 is False
