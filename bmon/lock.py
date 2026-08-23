"""跨进程采集互斥锁.

多套触发机制(Windows计划任务 / GUI内置调度器 / main.py scheduler / 手动CLI)
并存时, 保证同一时刻只有一轮采集在跑: 启动前以 O_EXCL 创建 fetch.lock,
内容为 pid+时间戳; 持锁进程异常退出未释放时, 锁在 ttl 后自动失效可被抢占.
"""
import json
import logging
import os
import time

log = logging.getLogger("bmon.lock")


def acquire(path, ttl=1800):
    """尝试获取锁; 成功返回 True(已持锁), 已被持有时返回 False."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    except OSError:
        pass
    # 清理过期锁(持锁进程崩溃未释放的情形)
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
        if time.time() - float(info.get("ts") or 0) > ttl:
            os.remove(path)
            log.info("清理过期锁: %s", path)
    except (OSError, ValueError):
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode())
    finally:
        os.close(fd)
    return True


def release(path):
    try:
        os.remove(path)
    except OSError:
        pass
