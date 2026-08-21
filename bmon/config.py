"""配置加载: YAML 用户配置与内置默认值深度合并; 相对路径基于项目根目录解析."""
import copy
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "accounts": [],
    "monitor": {
        "interval_minutes": 60,
        "request_interval_seconds": 2.5,
        "backfill_pages": 0,
        "track_since_days": 0,
        "active_days": 45,
        "stats_mode": "full",
        "listing_mode": "auto",
        "use_system_proxy": False,
        "feed_page_interval_seconds": 5,
        "meta_backfill_limit": 300,
        "timeout_seconds": 15,
        "max_retries": 3,
        "risk_control_wait_seconds": 30,
        "cookie": "",
    },
    "storage": {"db_path": "data/monitor.db"},
    "charts": {
        "auto": True,
        "periods": ["daily", "weekly", "monthly"],
        "periods_back": 12,
        "top_n": 15,
        "output_dir": "output/charts",
        "font": "",
        "auto_refresh_seconds": 300,
    },
    "logging": {"level": "INFO", "file": "data/monitor.log"},
}

CONFIG_TEMPLATE = """\
# ============================================================
#  B站官号视频播放量自动监测系统 配置 (YAML)
#  - 相对路径均基于项目根目录; 修改后重启程序生效
#  - 添加/更换账号: 复制一段 {mid, name}; mid 可用
#    `python main.py find --keyword 账号名` 联网查询
# ============================================================

accounts:
  - mid: 401742377          # 原神 (官方, 已核验)
    name: 原神
    enabled: true
  - mid: 1340190821         # 崩坏星穹铁道 (官方, 已核验)
    name: 崩坏星穹铁道
    enabled: true
  - mid: 1636034895         # 绝区零 (官方, 已核验)
    name: 绝区零
    enabled: true
  # 仅监测指定视频(手动模式, 可选):
  # - mid: 123456
  #   name: 某账号
  #   bvids: ["BV1xx411c7mD", "BV1ab411c7ba"]

monitor:
  interval_minutes: 60            # 自动监测间隔(分钟, 支持小数如 0.5)
  request_interval_seconds: 2.5   # 相邻API请求最小间隔(限速; 建议>=2, 过小有风控风险)
  backfill_pages: 0               # 首次抓取最多翻页数(每页50条, 0=不限制)
  track_since_days: 0             # 仅收录最近N天发布的视频(0=全部历史; 仅arc通道生效)
  active_days: 45                 # 仅对最近N天发布的视频逐周期采集指标(0=全部, 请求量大)
  stats_mode: full                # basic=仅列表接口(播放/评论); full=逐视频详情(点赞/投币等)
  listing_mode: auto              # 投稿清单通道: auto=arc优先/风控自动切动态流; 可强制 arc / feed
  use_system_proxy: false         # 是否走系统代理(默认直连; 数据中心代理出口IP易被B站风控)
  feed_page_interval_seconds: 5   # 动态流通道翻页间隔(该接口对频率敏感, 建议>=5)
  meta_backfill_limit: 300        # 每轮为新发现视频回填详情的最大数量(0=不限)
  timeout_seconds: 15
  max_retries: 3
  risk_control_wait_seconds: 30   # 触发风控(-352/412)后的冷却秒数
  cookie: ""                      # 可选: 粘贴浏览器Cookie(含SESSDATA); 留空=游客模式(零账号风险)

storage:
  db_path: data/monitor.db

charts:
  auto: true                      # 每轮采集后自动重建图表
  periods: [daily, weekly, monthly]  # 自动生成的周期(日/周/月)
  periods_back: 12                # 图表展示最近N个周期
  top_n: 15                       # Top视频数量
  output_dir: output/charts
  font: ""                        # 中文字体名; 留空自动探测(Windows: 微软雅黑)
  auto_refresh_seconds: 300       # index.html 自动刷新秒数(0=关闭)

logging:
  level: INFO
  file: data/monitor.log
"""


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _resolve(path):
    if path and not os.path.isabs(path):
        return os.path.join(ROOT, path)
    return path


def load_config(path):
    user = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULTS, user)
    cfg["storage"]["db_path"] = _resolve(cfg["storage"].get("db_path", ""))
    cfg["charts"]["output_dir"] = _resolve(cfg["charts"].get("output_dir", ""))
    cfg["logging"]["file"] = _resolve(cfg["logging"].get("file", ""))
    return cfg


def enabled_accounts(cfg):
    """返回启用且填写了 mid 的账号列表."""
    out = []
    for a in cfg.get("accounts") or []:
        if not a.get("enabled", True):
            continue
        if not a.get("mid"):
            continue
        out.append(a)
    return out


def account_labels(cfg):
    """mid -> 显示名(配置优先), 用于图表配色与图例的稳定顺序."""
    labels = {}
    for a in cfg.get("accounts") or []:
        if a.get("mid"):
            labels[int(a["mid"])] = a.get("name") or str(a["mid"])
    return labels
