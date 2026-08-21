"""通用小工具."""


def fmt_num(n):
    """数字压缩显示: 12345678 -> '1234.6万' / 1.2亿."""
    if n is None:
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1e8:
        return f"{n / 1e8:.2f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return f"{int(n)}"


def parse_len_seconds(text):
    """'1:02:03' / '10:23' -> 秒; 解析失败返回 None."""
    try:
        parts = [int(p) for p in str(text).split(":")]
    except (TypeError, ValueError):
        return None
    if not parts or any(p < 0 for p in parts):
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec
