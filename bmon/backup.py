"""SQLite 数据库备份: 使用官方 backup API, WAL 模式下也能得到一致快照."""
import logging
import os
import sqlite3
from datetime import datetime

log = logging.getLogger("bmon.backup")


def backup_db(db_path, keep=5):
    """备份到 data/backup/monitor-<时间戳>.db, 仅保留最近 keep 份; 返回备份路径."""
    if not os.path.exists(db_path):
        return None
    dest_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backup")
    os.makedirs(dest_dir, exist_ok=True)
    dst = os.path.join(dest_dir, f"monitor-{datetime.now():%Y%m%d-%H%M%S}.db")
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(dst)
    try:
        with out:
            src.backup(out)
    finally:
        out.close()
        src.close()
    if keep and keep > 0:
        backups = sorted(f for f in os.listdir(dest_dir) if f.endswith(".db"))
        for old in backups[:-keep]:
            try:
                os.remove(os.path.join(dest_dir, old))
            except OSError:
                pass
    return dst
