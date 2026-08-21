"""视频多条件筛选与表格展示/CSV导出; list 与 chart 命令共用同一套筛选参数."""
import argparse
import csv
import datetime as dt
import unicodedata

from .util import fmt_num


def add_filter_args(parser):
    g = parser.add_argument_group("筛选条件 (list / chart 通用)")
    g.add_argument("--account", action="append",
                   help="账号名或mid, 可多次或逗号分隔")
    g.add_argument("--keyword", help="标题关键词(包含匹配)")
    g.add_argument("--since", help="发布日期起 YYYY-MM-DD")
    g.add_argument("--until", help="发布日期止 YYYY-MM-DD)")
    g.add_argument("--min-views", type=int, help="最新播放量下限")
    g.add_argument("--max-views", type=int, help="最新播放量上限")
    g.add_argument("--min-seconds", type=int, help="时长下限(秒)")
    g.add_argument("--max-seconds", type=int, help="时长上限(秒)")
    g.add_argument("--tname", help="分区名包含(如 '单机游戏')")
    g.add_argument("--bvid", action="append", help="指定bvid(可多次)")
    g.add_argument("--sort", default="pubdate",
                   choices=["pubdate", "views", "likes", "growth", "duration"],
                   help="排序字段(默认 pubdate 降序)")
    g.add_argument("--asc", action="store_true", help="升序排列")
    g.add_argument("--limit", type=int, default=30, help="最多输出条数")


def _split_multi(values):
    out = []
    for v in values or []:
        out += [str(x).strip() for x in str(v).split(",") if str(x).strip()]
    return out


def _parse_date(s):
    s = str(s).strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            pass
    raise SystemExit(f"日期格式错误: {s} (应为 YYYY-MM-DD)")


def growth_of(row):
    a, b = row.get("latest_view"), row.get("first_view")
    if a is None or b is None:
        return None
    return max(0, a - b)


def apply_filters(rows, args):
    accounts = _split_multi(getattr(args, "account", None))
    accounts = {a if a.isdigit() else a.lower() for a in accounts}
    kw = (getattr(args, "keyword", None) or "").strip().lower()
    since = _parse_date(args.since) if getattr(args, "since", None) else None
    until = (_parse_date(args.until) + dt.timedelta(days=1)
             if getattr(args, "until", None) else None)
    tname = (getattr(args, "tname", None) or "").strip()
    bvids = {str(b).strip() for b in _split_multi(getattr(args, "bvid", None))}

    def match(r):
        if bvids and r["bvid"] not in bvids:
            return False
        if accounts:
            hay = {str(r.get("mid")), (r.get("account") or "").lower()}
            if not hay & accounts and not any(
                    a in (r.get("account") or "").lower() for a in accounts):
                return False
        if kw and kw not in (r.get("title") or "").lower():
            return False
        if tname and tname not in (r.get("tname") or ""):
            return False
        ts = r.get("created_ts")
        if ts:
            d = dt.datetime.fromtimestamp(ts)
            if since and d < since:
                return False
            if until and d >= until:
                return False
        elif since or until:
            return False
        v = r.get("latest_view")
        if getattr(args, "min_views", None) is not None:
            if v is None or v < args.min_views:
                return False
        if getattr(args, "max_views", None) is not None:
            if v is None or v > args.max_views:
                return False
        dur = r.get("duration")
        if getattr(args, "min_seconds", None) is not None:
            if dur is None or dur < args.min_seconds:
                return False
        if getattr(args, "max_seconds", None) is not None:
            if dur is None or dur > args.max_seconds:
                return False
        return True

    rows = [r for r in rows if match(r)]

    key_map = {
        "pubdate": lambda r: r.get("created_ts") or 0,
        "views": lambda r: r.get("latest_view") or 0,
        "likes": lambda r: r.get("latest_likes") or 0,
        "growth": lambda r: growth_of(r) or 0,
        "duration": lambda r: r.get("duration") or 0,
    }
    key = key_map[getattr(args, "sort", "pubdate") or "pubdate"]
    rows.sort(key=key, reverse=not getattr(args, "asc", False))

    limit = getattr(args, "limit", None)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


# ---------- 展示 ----------
def _dwidth(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in str(s))


def _trunc(s, width):
    s = str(s)
    if _dwidth(s) <= width:
        return s
    out = ""
    for ch in s:
        if _dwidth(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def _pad(s, width):
    return str(s) + " " * max(0, width - _dwidth(s))


def print_table(rows, total=None):
    total = total if total is not None else len(rows)
    cols = [
        ("created_at", "发布时间", 19, None),
        ("account", "账号", 14, None),
        ("title", "标题", 46, None),
        ("duration", "时长s", 7, None),
        ("latest_view", "播放", 11, fmt_num),
        ("growth", "增长", 10, fmt_num),
        ("latest_likes", "点赞", 9, fmt_num),
        ("tname", "分区", 10, None),
    ]
    table = []
    for r in rows:
        row = {}
        for key, _, _, fmt in cols:
            val = r.get(key)
            if key == "growth":
                val = growth_of(r)
            row[key] = "-" if val is None else (fmt(val) if fmt else str(val))
        table.append(row)

    widths = {}
    for key, head, w, _ in cols:
        widths[key] = max(w, _dwidth(head),
                          *[_dwidth(t[key]) for t in table]) if table else max(w, _dwidth(head))

    header = " | ".join(_pad(head, widths[key]) for key, head, _, _ in cols)
    print(header)
    print("-" * _dwidth(header))
    for t in table:
        line = []
        for key, _, w, _ in cols:
            cell = t[key]
            if key == "title":
                cell = _trunc(cell, w)
            line.append(_pad(cell, widths[key] if key != "title" else w))
        print(" | ".join(line))
    print(f"\n共 {total} 条" + (f", 显示前 {len(rows)} 条" if len(rows) < total else ""))


CSV_FIELDS = ["bvid", "url", "mid", "account", "title", "created_at", "created_ts",
              "duration", "tname", "tid", "latest_view", "first_view", "growth",
              "latest_likes", "latest_ts"]

SNAP_CSV_FIELDS = ["bvid", "url", "account", "title", "created_at",
                   "first_view", "latest_view", "growth", "first_ts", "latest_ts"]


def csv_content(rows, fields=None):
    """生成CSV文本(utf-8-sig), 供命令行写文件与Web导出共用."""
    import io
    fields = fields or CSV_FIELDS
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow([
            r.get(f) if f not in ("url", "growth") else
            (f"https://www.bilibili.com/video/{r.get('bvid')}" if r.get("bvid") else "")
            if f == "url" else growth_of(r)
            for f in fields])
    return buf.getvalue()


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        f.write(csv_content(rows))
    print(f"已导出 CSV: {path}")


# ---------- 快照查询展示 ----------
def print_snapshot_table(rows, ranged=True):
    if ranged:
        cols = [("created_at", "发布时间", 19, None),
                ("account", "账号", 14, None),
                ("title", "标题", 44, None),
                ("first_view", "期初播放", 12, fmt_num),
                ("latest_view", "期末播放", 12, fmt_num),
                ("growth", "增量", 11, fmt_num),
                ("latest_ts", "期末快照", 19, None)]
    else:
        cols = [("created_at", "发布时间", 19, None),
                ("account", "账号", 14, None),
                ("title", "标题", 46, None),
                ("latest_view", "该时刻播放", 13, fmt_num),
                ("latest_ts", "快照时间", 19, None)]
    table = []
    for r in rows:
        row = {}
        for key, _, _, fmt in cols:
            val = growth_of(r) if key == "growth" else r.get(key)
            row[key] = "-" if val is None else (fmt(val) if fmt else str(val))
        table.append(row)

    widths = {}
    for key, head, w, _ in cols:
        vals = [_dwidth(t[key]) for t in table] or [0]
        widths[key] = max(w, _dwidth(head), max(vals))

    header = " | ".join(_pad(head, widths[key]) for key, head, _, _ in cols)
    print(header)
    print("-" * _dwidth(header))
    for t in table:
        line = []
        for key, _, w, _ in cols:
            cell = t[key]
            if key == "title":
                cell = _trunc(cell, w)
            line.append(_pad(cell, widths[key] if key != "title" else w))
        print(" | ".join(line))
    print(f"\n共 {len(rows)} 条")


# ---------- Web/GUI 参数适配 ----------
def args_from_dict(d, default_limit=30, default_sort="views"):
    """把 dict / Flask request.args 转成与 CLI 筛选参数同名的 Namespace."""
    def _int(k):
        v = d.get(k)
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _list(k):
        getlist = getattr(d, "getlist", None)
        if getlist:
            v = getlist(k)
        else:
            v = d.get(k)
        if v is None:
            return None
        if isinstance(v, str):
            return [x for x in [v.strip()] if x] or None
        return [str(x) for x in v if str(x).strip()] or None

    def _str(k):
        v = d.get(k)
        return v if v not in (None, "") else None

    return argparse.Namespace(
        account=_list("account"), keyword=_str("keyword"),
        since=_str("since"), until=_str("until"),
        min_views=_int("min_views"), max_views=_int("max_views"),
        min_seconds=_int("min_seconds"), max_seconds=_int("max_seconds"),
        tname=_str("tname"), bvid=_list("bvid"),
        sort=_str("sort") or default_sort, asc=bool(_str("asc")),
        limit=_int("limit") if _int("limit") is not None else default_limit)
