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
