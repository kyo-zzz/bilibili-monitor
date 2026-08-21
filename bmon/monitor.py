"""监测调度: 单轮采集(fetch) 与 持续循环(run)."""
import json
import logging
import os
import time
import traceback
from datetime import datetime, timedelta

from .api import ApiError, BiliApi, RiskControlError
from .config import enabled_accounts
from .storage import Database, ts_str

log = logging.getLogger("bmon.monitor")


class Monitor:
    def __init__(self, cfg, db=None, api=None):
        self.cfg = cfg
        self.db = db or Database(cfg["storage"]["db_path"])
        self.api = api or BiliApi(cfg["monitor"])

    # ---------- 数据转换 ----------
    def _rows_from_items(self, mid, items):
        rows = []
        for it in items:
            created = it.get("created")
            rows.append({
                "bvid": it.get("bvid"),
                "mid": mid,
                "title": (it.get("title") or "").strip(),
                "created_ts": created,
                "created_at": (datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")
                               if created else None),
                "length_text": it.get("length"),
                "duration": it.get("duration"),
                "pic": it.get("pic"),
                "dyn_id": it.get("dyn_id"),
            })
        return [r for r in rows if r.get("bvid")]

    @staticmethod
    def _snap_from_vlist(it, ts):
        play = it.get("play")
        if isinstance(play, str):
            play = int(play) if play.isdigit() else None
        return dict(bvid=it.get("bvid"), ts=ts, view=play,
                    reply=it.get("comment") if isinstance(it.get("comment"), int) else None,
                    likes=None, coin=None, favorite=None, danmaku=None, share=None,
                    source="list")

    @staticmethod
    def _snap_from_detail(d, ts):
        st = d.get("stat") or {}

        def _int(x):
            return x if isinstance(x, int) else None

        return dict(bvid=d.get("bvid"), ts=ts, view=_int(st.get("view")),
                    likes=_int(st.get("like")), coin=_int(st.get("coin")),
                    favorite=_int(st.get("favorite")), danmaku=_int(st.get("danmaku")),
                    reply=_int(st.get("reply")), share=_int(st.get("share")),
                    source="detail")

    # ---------- 单账号采集 ----------
    def _collect_account(self, acc, now, full=False):
        mid = int(acc["mid"])
        label = acc.get("name") or str(mid)
        t0 = time.time()
        manual = [str(b).strip() for b in (acc.get("bvids") or []) if str(b).strip()]

        if manual:  # 手动模式: 仅监测指定bvid
            snaps = 0
            for bvid in manual:
                try:
                    d = self.api.video_detail(bvid)
                except ApiError as e:
                    log.warning("[%s] 视频 %s 获取失败: %s", label, bvid, e)
                    continue
                pub = d.get("pubdate")
                row = {
                    "bvid": d.get("bvid") or bvid,
                    "mid": (d.get("owner") or {}).get("mid") or mid,
                    "title": d.get("title"),
                    "created_ts": pub,
                    "created_at": (datetime.fromtimestamp(pub).strftime("%Y-%m-%d %H:%M:%S")
                                   if pub else None),
                    "length_text": None,
                    "duration": d.get("duration"),
                    "pic": d.get("pic"),
                }
                self.db.upsert_videos([row])
                self.db.update_video_meta(bvid, title=d.get("title"),
                                          created_ts=pub, duration=d.get("duration"),
                                          tid=d.get("tid"), tname=d.get("tname"))
                self.db.add_snapshot(**self._snap_from_detail(d, ts_str(now)))
                snaps += 1
            real = None
            try:
                card = self.api.account_card(mid)
                real = (card.get("card") or {}).get("name")
            except Exception:
                pass
            self.db.upsert_account(mid, name=label, real_name=real)
            log.info("[%s] 手动模式: 快照 %d 条, 耗时 %.0fs", label, snaps, time.time() - t0)
            return {"mid": mid, "label": label, "snapshots": snaps}

        # 自动模式: 拉取投稿清单(arc/search 优先, 风控时自动切动态流)
        mon = self.cfg["monitor"]
        track_since = int(mon.get("track_since_days") or 0)
        cutoff_ts = int((now - timedelta(days=track_since)).timestamp()) if track_since > 0 else 0
        known_old = set()
        first_run = not self.db.account_has_videos(mid)
        max_pages = 500
        if first_run and not full and int(mon.get("backfill_pages") or 0) > 0:
            max_pages = int(mon["backfill_pages"])
        if not full and not first_run:
            # 增量优化: 翻到"已入库且发布超过30天"的视频即可停止
            known_old = self.db.known_bvids_older_than(
                mid, int((now - timedelta(days=30)).timestamp()))

        def stop_fn(it):
            if cutoff_ts and (it.get("created") or 0) < cutoff_ts:
                return True
            if it.get("bvid") in known_old:
                return True
            return False

        # full=深度回填: feed通道从上次游标继续向历史翻页
        deep = bool(full)
        start_offset = self.db.get_feed_cursor(mid) if deep else None
        res = self.api.list_videos_auto(
            mid, stop_fn=stop_fn, max_pages=max_pages,
            mode=mon.get("listing_mode", "auto"), deep=deep,
            start_offset=start_offset)
        items, channel = res["items"], res["channel"]
        if channel == "feed" and deep:
            self.db.set_feed_cursor(mid, None if res["exhausted"] else res["cursor"])
            log.info("[%s] 深度回填: 本段 %d 条, %s", label, len(items),
                     "已翻完全部历史" if res["exhausted"] else "游标已保存, 可继续 fetch --full 续翻")
        rows = self._rows_from_items(mid, items)
        known = self.db.all_bvids(mid)
        new_bvids = {r["bvid"] for r in rows} - known if known else {r["bvid"] for r in rows}
        self.db.upsert_videos(rows)
        if channel == "arc":  # arc 自带发布时间, 直接标记元数据完成
            for bvid in new_bvids:
                self.db.mark_meta_done(bvid)

        real = items[0].get("author") if items else None
        fans = None
        try:
            card = self.api.account_card(mid)
            real = (card.get("card") or {}).get("name") or real
            fans = card.get("follower")
        except Exception as e:
            log.debug("[%s] 名片获取失败: %s", label, e)
        self.db.upsert_account(mid, name=label, real_name=real)
        log.info("[%s] 投稿清单(%s通道): 本轮 %d 条(新增 %d)%s",
                 label, channel, len(rows), len(new_bvids),
                 f" / 全站计数 {res['total']}" if channel == "arc" and res["total"] else "")

        # 步骤1: 为新发现的视频回填元数据(发布时间等)并留首份快照
        ts = ts_str(now)
        meta_limit = 0 if full else int(mon.get("meta_backfill_limit", 300) or 0)
        missing = self.db.videos_missing_meta(mid, limit=meta_limit)
        snapped = set()
        if missing:
            log.info("[%s] 回填 %d 个新视频的详情...", label, len(missing))
        for i, bvid in enumerate(missing, 1):
            try:
                d = self.api.video_detail(bvid)
            except ApiError as e:
                log.debug("[%s] %s 详情失败: %s", label, bvid, e)
                self.db.mark_meta_done(bvid)  # 业务错误不再重试
                continue
            except RiskControlError:
                raise
            self.db.update_video_meta(
                bvid, title=d.get("title"), created_ts=d.get("pubdate"),
                duration=d.get("duration"), tid=d.get("tid"), tname=d.get("tname"))
            self.db.add_snapshot(**self._snap_from_detail(d, ts))
            snapped.add(bvid)
            if i % 100 == 0:
                log.info("[%s] 元数据回填进度 %d/%d", label, i, len(missing))

        # 步骤2: 活跃窗口内视频的周期性快照
        active_days = int(mon.get("active_days") or 0)
        active_ts = int((now - timedelta(days=active_days)).timestamp()) if active_days > 0 else 0
        use_basic = (mon.get("stats_mode", "full") == "basic"
                     and channel == "arc")
        if use_basic:
            snaps = 0
            for it in items:
                if active_ts and (it.get("created") or 0) < active_ts:
                    continue
                self.db.add_snapshot(**self._snap_from_vlist(it, ts))
                snaps += 1
        else:
            active = self.db.active_bvids(mid, active_ts) if active_ts else \
                self.db.all_bvids(mid)
            snaps = 0
            for bvid in active:
                if bvid in snapped:
                    snaps += 1
                    continue
                try:
                    d = self.api.video_detail(bvid)
                except ApiError as e:
                    log.debug("[%s] %s 详情失败: %s", label, bvid, e)
                    continue
                self.db.add_snapshot(**self._snap_from_detail(d, ts))
                snaps += 1
        self.db.commit()
        log.info("[%s] 快照 %d 条(活跃窗口 %d 天), 耗时 %.0fs",
                 label, snaps + len(snapped), active_days or 0, time.time() - t0)
        return {"mid": mid, "label": label, "snapshots": snaps + len(snapped)}

    # ---------- 周期 ----------
    def run_once(self, full=False):
        now = datetime.now()
        accs = enabled_accounts(self.cfg)
        if not accs:
            log.warning("config.yaml 中没有启用的账号, 跳过本轮")
            return []
        log.info("====== 开始采集 @ %s ======", now.strftime("%Y-%m-%d %H:%M:%S"))
        t0 = time.time()
        v_before = self.db.con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        s_before = self.db.snapshot_count()
        results = []
        for acc in accs:
            try:
                results.append(self._collect_account(acc, now, full=full))
            except RiskControlError as e:
                log.error("账号 %s 触发风控, 本轮跳过: %s", acc.get("name"), e)
            except ApiError as e:
                log.error("账号 %s 接口异常, 本轮跳过: %s", acc.get("name"), e)
        self._auto_charts()
        self._write_state(now, results)
        v_after = self.db.con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        s_after = self.db.snapshot_count()
        detail = " · ".join(f"{r['label']}: 快照{r['snapshots']}条"
                            for r in results) or "无有效账号"
        log.info("====== 本轮采集完成: 耗时%.0fs | 视频%d(新增+%d) | 快照%d(新增+%d) | %s ======",
                 time.time() - t0, v_after, v_after - v_before,
                 s_after, s_after - s_before, detail)
        return results

    def _auto_charts(self):
        cc = self.cfg["charts"]
        if not cc.get("auto"):
            return
        try:
            from . import charts
            charts.setup_font(cc.get("font"))
            rows = self.db.videos_with_stats()
            made = []
            for kind in cc.get("periods") or []:
                made.append(charts.make_dashboard(self.db, self.cfg, kind, rows))
            charts.write_index(self.cfg)
            for p in made:
                log.info("图表已更新: %s", p)
        except Exception:
            log.error("自动生成图表失败:\n%s", traceback.format_exc())

    def _write_state(self, now, results):
        state = {
            "last_cycle_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": results,
            "videos_total": self.db.con.execute(
                "SELECT COUNT(*) FROM videos").fetchone()[0],
            "snapshots_total": self.db.snapshot_count(),
        }
        state_path = os.path.join(os.path.dirname(self.cfg["storage"]["db_path"]),
                                  "state.json")
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.debug("写入 state.json 失败: %s", e)

    def run_loop(self, interval_minutes=None, max_cycles=None):
        interval = 60.0 * (interval_minutes if interval_minutes is not None
                           else float(self.cfg["monitor"].get("interval_minutes", 60)))
        interval = max(5.0, interval)
        log.info("进入持续监测模式: 间隔 %.1f 分钟 (Ctrl+C 退出)", interval / 60)
        n = 0
        while True:
            t0 = time.time()
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.error("本轮采集出现未预期异常(循环继续):\n%s", traceback.format_exc())
            n += 1
            if max_cycles and n >= max_cycles:
                log.info("已运行 %d 轮, 按参数退出", n)
                break
            remain = interval - (time.time() - t0)
            if remain > 0:
                time.sleep(remain)
