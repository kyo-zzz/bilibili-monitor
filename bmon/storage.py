"""SQLite 存储: 账号/视频元数据 + 播放量等指标的历史快照."""
import os
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    mid         INTEGER PRIMARY KEY,
    name        TEXT,
    real_name   TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    bvid        TEXT PRIMARY KEY,
    mid         INTEGER NOT NULL,
    title       TEXT,
    created_ts  INTEGER,
    created_at  TEXT,
    length_text TEXT,
    duration    INTEGER,
    tid         INTEGER,
    tname       TEXT,
    pic         TEXT,
    dyn_id      TEXT,
    meta_done   INTEGER DEFAULT 0,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_mid     ON videos(mid);
CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created_ts);
CREATE INDEX IF NOT EXISTS idx_videos_meta    ON videos(mid, meta_done);
CREATE TABLE IF NOT EXISTS snapshots (
    bvid     TEXT NOT NULL,
    ts       TEXT NOT NULL,
    view     INTEGER,
    likes    INTEGER,
    coin     INTEGER,
    favorite INTEGER,
    danmaku  INTEGER,
    reply    INTEGER,
    share    INTEGER,
    source   TEXT,
    PRIMARY KEY (bvid, ts)
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);
"""


def ts_str(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self):
        """为旧库补充新增列."""
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(videos)")}
        for col, ddl in (("dyn_id", "TEXT"), ("meta_done", "INTEGER DEFAULT 0")):
            if col not in cols:
                self.con.execute(f"ALTER TABLE videos ADD COLUMN {col} {ddl}")
        acols = {r["name"] for r in self.con.execute("PRAGMA table_info(accounts)")}
        if "feed_cursor" not in acols:
            self.con.execute("ALTER TABLE accounts ADD COLUMN feed_cursor TEXT")

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    def commit(self):
        self.con.commit()

    # ---------- 账号 ----------
    def upsert_account(self, mid, name=None, real_name=None):
        now = ts_str()
        row = self.con.execute("SELECT mid FROM accounts WHERE mid=?", (mid,)).fetchone()
        if row is None:
            self.con.execute(
                "INSERT INTO accounts(mid,name,real_name,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (mid, name, real_name, now, now))
        else:
            self.con.execute(
                "UPDATE accounts SET last_seen=?, name=COALESCE(?,name), "
                "real_name=COALESCE(?,real_name) WHERE mid=?",
                (now, name, real_name, mid))
        self.con.commit()

    def get_feed_cursor(self, mid):
        row = self.con.execute("SELECT feed_cursor FROM accounts WHERE mid=?",
                               (mid,)).fetchone()
        return row["feed_cursor"] if row else None

    def set_feed_cursor(self, mid, cursor):
        """cursor 为 None 表示已翻完(深度回填完成)."""
        self.con.execute("UPDATE accounts SET feed_cursor=? WHERE mid=?", (cursor, mid))
        self.con.commit()

    # ---------- 视频 ----------
    def upsert_videos(self, rows):
        """rows: [{bvid,mid,title,created_ts,created_at,length_text,duration,pic,dyn_id}]"""
        now = ts_str()
        self.con.executemany(
            """INSERT INTO videos(bvid,mid,title,created_ts,created_at,length_text,
                                  duration,pic,dyn_id,first_seen,last_seen)
               VALUES(:bvid,:mid,:title,:created_ts,:created_at,:length_text,
                      :duration,:pic,:dyn_id,:first_seen,:last_seen)
               ON CONFLICT(bvid) DO UPDATE SET
                 title=excluded.title, created_ts=COALESCE(excluded.created_ts, videos.created_ts),
                 created_at=COALESCE(excluded.created_at, videos.created_at),
                 length_text=COALESCE(excluded.length_text, videos.length_text),
                 duration=COALESCE(excluded.duration, videos.duration),
                 pic=excluded.pic,
                 dyn_id=COALESCE(excluded.dyn_id, videos.dyn_id),
                 last_seen=excluded.last_seen""",
            [dict(r, first_seen=now, last_seen=now) for r in rows])
        self.con.commit()

    def update_video_meta(self, bvid, title=None, created_ts=None, duration=None,
                          tid=None, tname=None):
        """用 view 接口结果补齐元数据, 并标记 meta_done=1."""
        created_at = (datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d %H:%M:%S")
                      if created_ts else None)
        self.con.execute(
            "UPDATE videos SET title=COALESCE(?,title), "
            "created_ts=COALESCE(?,created_ts), created_at=COALESCE(?,created_at), "
            "duration=COALESCE(?,duration), tid=COALESCE(?,tid), "
            "tname=COALESCE(?,tname), meta_done=1 WHERE bvid=?",
            (title, created_ts, created_at, duration, tid, tname, bvid))
        self.con.commit()

    def mark_meta_done(self, bvid):
        self.con.execute("UPDATE videos SET meta_done=1 WHERE bvid=?", (bvid,))
        self.con.commit()

    def add_snapshot(self, bvid, ts, view=None, likes=None, coin=None,
                     favorite=None, danmaku=None, reply=None, share=None,
                     source=""):
        self.con.execute(
            "INSERT OR IGNORE INTO snapshots(bvid,ts,view,likes,coin,favorite,"
            "danmaku,reply,share,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bvid, ts, view, likes, coin, favorite, danmaku, reply, share, source))

    def videos_missing_meta(self, mid, limit=0):
        """待回填元数据(发布时间等)的bvid; limit=0 表示不限."""
        q = "SELECT bvid FROM videos WHERE mid=? AND meta_done=0 ORDER BY rowid DESC"
        args = [mid]
        if limit and limit > 0:
            q += " LIMIT ?"
            args.append(limit)
        return [r["bvid"] for r in self.con.execute(q, args)]

    def active_bvids(self, mid, since_ts):
        return [r["bvid"] for r in self.con.execute(
            "SELECT bvid FROM videos WHERE mid=? AND created_ts>=? ORDER BY created_ts DESC",
            (mid, since_ts))]

    def update_video_detail(self, bvid, title=None, duration=None, tid=None, tname=None):
        self.con.execute(
            "UPDATE videos SET title=COALESCE(?,title), duration=COALESCE(?,duration), "
            "tid=COALESCE(?,tid), tname=COALESCE(?,tname) WHERE bvid=?",
            (title, duration, tid, tname, bvid))
        self.con.commit()

    def all_bvids(self, mid):
        rows = self.con.execute("SELECT bvid FROM videos WHERE mid=?", (mid,)).fetchall()
        return {r["bvid"] for r in rows}

    def known_bvids_older_than(self, mid, ts_cutoff):
        rows = self.con.execute(
            "SELECT bvid FROM videos WHERE mid=? AND created_ts<?", (mid, ts_cutoff)).fetchall()
        return {r["bvid"] for r in rows}

    def account_has_videos(self, mid):
        return self.con.execute(
            "SELECT COUNT(*) FROM videos WHERE mid=?", (mid,)).fetchone()[0] > 0

    def video_count(self, mid):
        return self.con.execute(
            "SELECT COUNT(*) FROM videos WHERE mid=?", (mid,)).fetchone()[0]

    def snapshot_count(self):
        return self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    def last_snapshot_ts(self):
        row = self.con.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        return row[0]

    # ---------- 查询 ----------
    def videos_with_stats(self):
        """视频元数据 + 最新/最早快照指标, 供筛选与图表使用."""
        sql = """
        SELECT v.bvid, v.mid, v.title, v.created_ts, v.created_at, v.duration,
               v.tid, v.tname,
               COALESCE(a.real_name, a.name, CAST(v.mid AS TEXT)) AS account,
               ls.view  AS latest_view,  ls.likes AS latest_likes, ls.ts AS latest_ts,
               fs.view  AS first_view
        FROM videos v
        LEFT JOIN accounts a ON a.mid = v.mid
        LEFT JOIN snapshots ls ON ls.bvid = v.bvid
             AND ls.ts = (SELECT MAX(ts) FROM snapshots s2 WHERE s2.bvid = v.bvid)
        LEFT JOIN snapshots fs ON fs.bvid = v.bvid
             AND fs.ts = (SELECT MIN(ts) FROM snapshots s3 WHERE s3.bvid = v.bvid)
        """
        return [dict(r) for r in self.con.execute(sql).fetchall()]

    def snapshots_between(self, start_ts, end_ts, bvids=None):
        q = "SELECT bvid, ts, view FROM snapshots WHERE ts>=? AND ts<?"
        args = [start_ts, end_ts]
        if bvids is not None:
            if not bvids:
                return []
            q += " AND bvid IN (%s)" % ",".join("?" * len(bvids))
            args += list(bvids)
        q += " ORDER BY ts"
        return [dict(r) for r in self.con.execute(q, args).fetchall()]

    def snapshot_report(self, ts_to, ts_from=None):
        """快照查询: 任意时刻/时段各视频的播放量.

        - 仅 ts_to: 期末 = <=ts_to 的最近快照(期初列与期末相同);
        - ts_from + ts_to: 期初 = <=ts_from 最近快照, 期末 = <=ts_to 最近快照, 增量=差值.
        字段命名与 videos_with_stats 对齐(latest_view/first_view), 可直接复用筛选器.
        """
        sql = """
        WITH s_to AS (
            SELECT bvid, view, MAX(ts) AS ts FROM snapshots WHERE ts<=:t_to GROUP BY bvid
        ), s_from AS (
            SELECT bvid, view, MAX(ts) AS ts FROM snapshots WHERE ts<=:t_from GROUP BY bvid
        )
        SELECT v.bvid, v.mid, v.title, v.created_ts, v.created_at, v.duration,
               v.tname,
               COALESCE(a.real_name, a.name, CAST(v.mid AS TEXT)) AS account,
               st.view AS latest_view, st.ts AS latest_ts,
               sf.view AS first_view, sf.ts AS first_ts
        FROM videos v
        LEFT JOIN accounts a ON a.mid=v.mid
        JOIN s_to st ON st.bvid=v.bvid
        LEFT JOIN s_from sf ON sf.bvid=v.bvid
        """
        params = {"t_to": ts_to, "t_from": ts_from or ts_to}
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]
