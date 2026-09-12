# -*- coding: utf-8 -*-
"""BGM 节拍分析与场景对齐.

每首 BGM 分析一次并缓存标记(<音频名>.beats.json, 按文件大小+修改时间失效),
之后生成视频直接读取标记, 无需重复计算.

- 节拍检测: 优先 librosa(节拍+重拍); 不可用时回退到能量包络峰值(纯 numpy);
- scene_bounds: 把各幕边界按比例铺在 BGM 时长上, 并吸附到最近的节拍,
  保证分镜切换踩在点上且每幕不短于 min_scene 秒.
"""
import json
import logging
import os
import subprocess
import tempfile
import wave

log = logging.getLogger("bmon.bgm")

SIDECAR_VER = 1


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def _decode_wav(path, sr=22050):
    """任意音频 → 单声道 wav 临时文件(经 ffmpeg 解码). 返回 (wav路径, 时长秒)."""
    fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    fd.close()
    cmd = [_ffmpeg(), "-y", "-i", path, "-ac", "1", "-ar", str(sr), "-vn",
           "-f", "wav", fd.name]
    subprocess.run(cmd, capture_output=True)
    with wave.open(fd.name, "rb") as w:
        dur = w.getnframes() / float(w.getframerate() or sr)
    return fd.name, dur


def _beats_librosa(wav, sr):
    import librosa
    import numpy as np
    y, sr = librosa.load(wav, sr=sr)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=False)
    return (sorted(float(b) for b in beats),
            sorted(float(o) for o in onsets),
            float(tempo) if tempo and not np.isnan(tempo).any() else 0.0)


def _beats_energy(wav, sr):
    """兜底: RMS 能量包络的自适应峰值检测(纯 numpy)."""
    import numpy as np
    with wave.open(wav, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop = 512
    env = np.sqrt(np.mean(x[:len(x) // hop * hop].reshape(-1, hop) ** 2, axis=1))
    if len(env) < 8:
        return [], [], 0.0
    kernel = max(3, int(0.4 * sr / hop) | 1)
    pad = kernel // 2
    base = np.convolve(env, np.ones(kernel) / kernel, mode="same")
    strong = env > base * 1.35
    idx = [i for i in range(1, len(env) - 1)
           if strong[i] and env[i] >= env[i - 1] and env[i] >= env[i + 1]]
    # 最小间隔 0.25s 去抖
    min_gap = max(1, int(0.25 * sr / hop))
    picked, last = [], -10 ** 9
    for i in idx:
        if i - last >= min_gap:
            picked.append(i)
            last = i
    times = [round(i * hop / sr, 3) for i in picked]
    return times, list(times), 0.0


def analyze(path, force=False):
    """分析 BGM 节拍, 返回标记 dict(带缓存)."""
    side = path + ".beats.json"
    size = os.path.getsize(path)
    mtime = int(os.path.getmtime(path))
    if not force and os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("ver") == SIDECAR_VER and d.get("size") == size \
                    and d.get("mtime") == mtime:
                return d
        except Exception:
            pass
    wav, dur = _decode_wav(path)
    try:
        try:
            beats, onsets, tempo = _beats_librosa(wav, 22050)
            engine = "librosa"
        except Exception as e:
            log.warning("librosa 节拍分析不可用(%s), 使用能量包络兜底", e)
            beats, onsets, tempo = _beats_energy(wav, 22050)
            engine = "energy"
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    d = {"path": os.path.basename(path), "size": size, "mtime": mtime,
         "duration": round(dur, 3), "tempo": round(tempo, 2),
         "beats": [round(b, 3) for b in beats],
         "onsets": [round(o, 3) for o in onsets][:400],
         "engine": engine, "ver": SIDECAR_VER}
    try:
        with open(side, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except OSError as e:
        log.warning("节拍标记写入失败: %s", e)
    log.info("BGM 节拍分析完成: %s | %.1fs | %d 拍 | 引擎 %s",
             os.path.basename(path), dur, len(beats), engine)
    return d


def scene_bounds(beats, weights, target, min_scene=2.2):
    """把各幕边界(按权重比例铺在 target 时长上)吸附到最近的节拍.

    返回边界列表 [0, b1, ..., target](秒), 长度 = len(weights)+1;
    保证单调递增且每幕不短于 min_scene 秒. 无节拍时退化为纯比例.
    """
    beats = sorted(b for b in beats if 0.0 < b < target - 0.5)
    total_w = sum(weights) or 1.0
    raw, acc = [0.0], 0.0
    for w in weights:
        acc += w
        raw.append(acc / total_w * target)
    bounds = [0.0]
    remain = len(raw) - 1
    for idx in range(1, len(raw) - 1):
        b = raw[idx]
        if beats:
            b = min(beats, key=lambda t: abs(t - b))
        remain -= 1
        b = max(b, bounds[-1] + min_scene)
        b = min(b, target - min_scene * remain)
        bounds.append(b)
    bounds.append(target)
    return bounds
