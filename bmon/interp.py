"""展示用插值估算: 由既有快照序列推算任意时刻的播放量.

仅供图表等展示聚合使用, 结果绝不写入 snapshots 表.
规则:
- 目标时刻落在两个快照之间 → 线性插值;
- 超出最后一个快照(如"今天还没采集") → 沿最后一段斜率线性外推,
  斜率为负按 0 处理(播放量不回退), 外推超过 max_horizon 后持有末值;
- 早于第一个快照 → 持有首值.

返回 (估算值, 是否含估算/外推成分).
"""
import bisect
from datetime import timedelta

DEFAULT_MAX_HORIZON = timedelta(days=7)


def estimate_at(points, t, max_horizon=DEFAULT_MAX_HORIZON):
    """points: [(datetime, value)]; t: datetime; 返回 (value, estimated)."""
    pts = sorted((p[0], p[1]) for p in points if p[1] is not None)
    if not pts:
        return None, False
    if t <= pts[0][0]:
        return pts[0][1], False
    if t >= pts[-1][0]:
        span = (t - pts[-1][0]).total_seconds()
        if len(pts) >= 2 and span <= max_horizon.total_seconds():
            dt = (pts[-1][0] - pts[-2][0]).total_seconds()
            slope = ((pts[-1][1] - pts[-2][1]) / dt) if dt > 0 else 0.0
            if slope < 0:
                slope = 0.0
            return pts[-1][1] + slope * span, True
        return pts[-1][1], True
    times = [p[0] for p in pts]
    i = bisect.bisect_left(times, t)
    t0, v0 = pts[i - 1]
    t1, v1 = pts[i]
    if t1 == t0:
        return v0, False
    frac = (t - t0).total_seconds() / (t1 - t0).total_seconds()
    return v0 + (v1 - v0) * frac, False
