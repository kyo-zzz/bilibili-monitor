"""Bilibili Web API 客户端.

安全与反风控设计(模拟真实浏览器访问, 将干扰与风险降到最低):
- curl_cffi 模拟 Chrome TLS/JA3 指纹(未安装时回退普通 requests);
- 完整游客身份初始化: buvid 指纹 Cookie + _uuid/b_lsid/buvid_fp 生成 +
  ExClimbWuzhi 设备指纹上报激活(官方 web 端 gaia 风控流程), 默认零登录态;
- 投稿列表接口按官方 WBI 算法签名, 并携带 web 端 dm_img 设备参数与空间页 Referer;
- 请求间隔限速 + 随机抖动; 风控(412/429/-352)触发冷却并重建游客身份后重试;
- 网络错误指数退避.
"""
import hashlib
import hmac
import io
import json
import logging
import random
import re
import time
import urllib.parse

try:  # Chrome TLS 指纹伪装(强烈推荐, 否则部分接口会被 412 拦截)
    from curl_cffi import requests as _transport
    _IMPERSONATE = "chrome124"
except ImportError:  # pragma: no cover
    import requests as _transport
    _IMPERSONATE = None

from .util import parse_len_seconds

log = logging.getLogger("bmon.api")

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
FINGER_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
ARC_SEARCH_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
FEED_SPACE_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
CARD_URL = "https://api.bilibili.com/x/web-interface/card"
USER_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
EXCLIMB_URL = "https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi"
TICKET_URL = "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
HOME_URL = "https://www.bilibili.com/"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 官方 web 端 WBI 混淆密钥重排表 ( bilibili-API-collect )
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

FILTED_CHARS = re.compile(r"[!'()*]")

# arc/search 等强风控接口所需的 web 端设备参数(静态即可通过校验)
DM_IMG_PARAMS = {
    "platform": "web",
    "web_location": "1550101",
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChXaW5kb3dzKQ==",
    "dm_cover_img_str": ("QU5HTEUgKEFJUCwgTlZpZGlhIFJ0eCAoMHgwMDAwMjcyYSkg"
                         "RGlyZWN0M0QxMSB2c19fNV8wIHBzXzVfMCwgRDNEMTFGci53"
                         "aW5kb3cpIChOSURJQSBHZUZvcmNlIFJUIDMwNjAp"),
    "dm_img_inter": '{"ds":[],"wh":[3121,3121,80,80],"of":[0,0,0,0]}',
}


class ApiError(Exception):
    """接口返回非零 code(非风控类), 如 -404 稿件不存在."""

    def __init__(self, code, message):
        super().__init__(f"code={code} {message}")
        self.code = code
        self.message = message


class RiskControlError(Exception):
    """触发B站风控(HTTP 412/429 或 code -352/-412)."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def _mixin_key(orig):
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _strip_html(text):
    return re.sub(r"<.*?>", "", str(text or ""))


# ---------- 游客身份辅助 ----------
def _gen_uuid():
    """生成 _uuid Cookie 值(格式与 web 端一致)."""
    t = int(time.time()) % 100000
    mp = list("123456789ABCDEF") + ["10"]
    parts = ["".join(random.choice(mp) for _ in range(n)) for n in (8, 4, 4, 4, 12)]
    return "-".join(parts) + str(t).ljust(5, "0") + "infoc"


def _gen_b_lsid():
    head = "".join(random.choice("0123456789ABCDEF") for _ in range(8))
    return f"{head}_{hex(int(time.time()))[2:].upper()}"


def _murmur3_x64_128(source: bytes, seed: int):
    """murmur3 x64 128位哈希, 用于生成 buvid_fp."""
    MOD = 1 << 64
    C1 = 0x87C37B91114253D5
    C2 = 0x4CF5AD432745937F
    C3 = 0x52DCE729
    C4 = 0x38495AB5
    R1, R2, R3, M = 27, 31, 33, 5

    def rotl(x, k):
        b = bin(x & (MOD - 1))[2:].rjust(64, "0")
        return int(b[k:] + b[:k], 2)

    def fmix64(k):
        k ^= k >> 33
        k = k * 0xFF51AFD7ED558CCD % MOD
        k ^= k >> 33
        k = k * 0xC4CEB9FE1A85EC53 % MOD
        k ^= k >> 33
        return k

    h1 = h2 = seed
    processed = 0
    buf = io.BytesIO(source)
    while True:
        read = buf.read(16)
        processed += len(read)
        if len(read) == 16:
            k1 = int.from_bytes(read[:8], "little")
            k2 = int.from_bytes(read[8:], "little")
            h1 ^= rotl(k1 * C1 % MOD, R2) * C2 % MOD
            h1 = (rotl(h1, R1) + h2) * M + C3 % MOD
            h2 ^= rotl(k2 * C2 % MOD, R3) * C1 % MOD
            h2 = (rotl(h2, R2) + h1) * M + C4 % MOD
        elif len(read) == 0:
            h1 ^= processed
            h2 ^= processed
            h1 = (h1 + h2) % MOD
            h2 = (h2 + h1) % MOD
            h1 = fmix64(h1)
            h2 = fmix64(h2)
            h1 = (h1 + h2) % MOD
            h2 = (h2 + h1) % MOD
            return (h2 << 64) | h1
        else:
            k1 = k2 = 0
            if len(read) >= 15:
                k2 ^= read[14] << 48
            if len(read) >= 14:
                k2 ^= read[13] << 40
            if len(read) >= 13:
                k2 ^= read[12] << 32
            if len(read) >= 12:
                k2 ^= read[11] << 24
            if len(read) >= 11:
                k2 ^= read[10] << 16
            if len(read) >= 10:
                k2 ^= read[9] << 8
            if len(read) >= 9:
                k2 ^= read[8]
                k2 = rotl(k2 * C2 % MOD, R3) * C1 % MOD
                h2 ^= k2
            if len(read) >= 8:
                k1 ^= read[7] << 56
            if len(read) >= 7:
                k1 ^= read[6] << 48
            if len(read) >= 6:
                k1 ^= read[5] << 40
            if len(read) >= 5:
                k1 ^= read[4] << 32
            if len(read) >= 4:
                k1 ^= read[3] << 24
            if len(read) >= 3:
                k1 ^= read[2] << 16
            if len(read) >= 2:
                k1 ^= read[1] << 8
            if len(read) >= 1:
                k1 ^= read[0]
                k1 = rotl(k1 * C1 % MOD, R2) * C2 % MOD
                h1 ^= k1


def _buvid_fp(user_agent: str) -> str:
    m = _murmur3_x64_128(user_agent.encode("ascii", "ignore"), 31)
    return f"{m & ((1 << 64) - 1):x}{m >> 64:x}"


def _exclimbwuzhi_payload(user_agent: str, uuid: str) -> dict:
    """ExClimbWuzhi 设备指纹上报payload(静态浏览器指纹, 参考开源实现)."""
    return {
        "3064": 1,
        "5062": str(int(time.time() * 1000)),
        "03bf": "https%3A%2F%2Fwww.bilibili.com%2F",
        "39c8": "333.1007.fp.risk",
        "34f1": "",
        "d402": "",
        "654a": "",
        "6e7c": "1699x794",
        "3c43": {
            "2673": 0, "5766": 32, "6527": 0, "7003": 1, "807e": 1,
            "b8ce": user_agent, "641c": 0, "07a4": "zh-CN", "1c57": 32,
            "0bd0": 20, "748e": [960, 1707], "d61f": [912, 1707], "fc9d": -480,
            "6aa9": "Asia/Shanghai", "75b8": 1, "3b21": 1, "8a1c": 0,
            "d52f": "not available", "adca": "Win32",
            "80c9": [
                ["PDF Viewer", "Portable Document Format",
                 [["application/pdf", "pdf"], ["text/pdf", "pdf"]]],
                ["Chrome PDF Viewer", "Portable Document Format",
                 [["application/pdf", "pdf"], ["text/pdf", "pdf"]]],
                ["Chromium PDF Viewer", "Portable Document Format",
                 [["application/pdf", "pdf"], ["text/pdf", "pdf"]]],
                ["Microsoft Edge PDF Viewer", "Portable Document Format",
                 [["application/pdf", "pdf"], ["text/pdf", "pdf"]]],
                ["WebKit built-in PDF", "Portable Document Format",
                 [["application/pdf", "pdf"], ["text/pdf", "pdf"]]],
            ],
            "13ab": "EPQAAAAASUVORK5CYII=",
            "bfe9": "//TgNIfAAAAAZJREFUAwBde+3wgcxEHQAAAABJRU5ErkJggg==",
            "a3c1": [
                "extensions:ANGLE_instanced_arrays;EXT_blend_minmax;"
                "EXT_clip_control;EXT_color_buffer_half_float;EXT_depth_clamp;"
                "EXT_disjoint_timer_query;EXT_float_blend;EXT_frag_depth;"
                "EXT_polygon_offset_clamp;EXT_shader_texture_lod;"
                "EXT_texture_compression_bptc;EXT_texture_compression_rgtc;"
                "EXT_texture_filter_anisotropic;EXT_texture_mirror_clamp_to_edge;"
                "EXT_s_rgb;KHR_parallel_shader_compile;OES_element_index_uint;"
                "OES_fbo_render_mipmap;OES_standard_derivatives;OES_texture_float;"
                "OES_texture_float_linear;OES_texture_half_float;"
                "OES_texture_half_float_linear;OES_vertex_array_object;"
                "WEBGL_blend_func_extended;WEBGL_color_buffer_float;"
                "WEBGL_compressed_texture_s3tc;WEBGL_compressed_texture_s3tc_srgb;"
                "WEBGL_debug_renderer_info;WEBGL_debug_shaders;WEBGL_depth_texture;"
                "WEBGL_draw_buffers;WEBGL_lose_context;WEBGL_multi_draw;"
                "WEBGL_polygon_mode",
                "webgl aliased line width range:[1, 1]",
                "webgl aliased point size range:[1, 1024]",
                "webgl alpha bits:8", "webgl antialiasing:yes",
                "webgl blue bits:8", "webgl depth bits:24", "webgl green bits:8",
                "webgl max anisotropy:16",
                "webgl max combined texture image units:32",
                "webgl max cube map texture size:16384",
                "webgl max fragment uniform vectors:1024",
                "webgl max render buffer size:16384",
                "webgl max texture image units:16",
                "webgl max texture size:16384", "webgl max varying vectors:30",
                "webgl max vertex attribs:16",
                "webgl max vertex texture image units:16",
                "webgl max vertex uniform vectors:4095",
                "webgl max viewport dims:[32767, 32767]",
                "webgl red bits:8", "webgl renderer:WebKit WebGL",
                "webgl shading language version:WebGL GLSL ES 1.0 "
                "(OpenGL ES GLSL ES 1.0 Chromium)",
                "webgl stencil bits:0", "webgl vendor:WebKit",
                "webgl version:WebGL 1.0 (OpenGL ES 2.0 Chromium)",
                "webgl unmasked vendor:Google Inc. (NVIDIA)",
                "webgl unmasked renderer:ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 "
                "Laptop GPU (0x000028E0) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "webgl vertex shader high float precision:23",
                "webgl vertex shader high float precision rangeMin:127",
                "webgl vertex shader high float precision rangeMax:127",
                "webgl vertex shader medium float precision:23",
                "webgl vertex shader medium float precision rangeMin:127",
                "webgl vertex shader medium float precision rangeMax:127",
                "webgl vertex shader low float precision:23",
                "webgl vertex shader low float precision rangeMin:127",
                "webgl vertex shader low float precision rangeMax:127",
                "webgl fragment shader high float precision:23",
                "webgl fragment shader high float precision rangeMin:127",
                "webgl fragment shader high float precision rangeMax:127",
                "webgl fragment shader medium float precision:23",
                "webgl fragment shader medium float precision rangeMin:127",
                "webgl fragment shader medium float precision rangeMax:127",
                "webgl fragment shader low float precision:23",
                "webgl fragment shader low float precision rangeMin:127",
                "webgl fragment shader low float precision rangeMax:127",
                "webgl vertex shader high int precision:0",
                "webgl vertex shader high int precision rangeMin:31",
                "webgl vertex shader high int precision rangeMax:30",
                "webgl vertex shader medium int precision:0",
                "webgl vertex shader medium int precision rangeMin:31",
                "webgl vertex shader medium int precision rangeMax:30",
                "webgl vertex shader low int precision:0",
                "webgl vertex shader low int precision rangeMin:31",
                "webgl vertex shader low int precision rangeMax:30",
                "webgl fragment shader high int precision:0",
                "webgl fragment shader high int precision rangeMin:31",
                "webgl fragment shader high int precision rangeMax:30",
                "webgl fragment shader medium int precision:0",
                "webgl fragment shader medium int precision rangeMin:31",
                "webgl fragment shader medium int precision rangeMax:30",
                "webgl fragment shader low int precision:0",
                "webgl fragment shader low int precision rangeMin:31",
                "webgl fragment shader low int precision rangeMax:30",
            ],
            "6bc5": ("Google Inc. (NVIDIA)~ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 "
                     "Laptop GPU (0x000028E0) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            "ed31": 0, "72bd": 0, "097b": 0, "52cd": [0, 0, 0],
            "a658": [
                "Arial", "Arial Black", "Arial Narrow", "Book Antiqua",
                "Bookman Old Style", "Calibri", "Cambria", "Cambria Math",
                "Century", "Century Gothic", "Century Schoolbook", "Comic Sans MS",
                "Consolas", "Courier", "Courier New", "Georgia", "Helvetica",
                "Impact", "Lucida Bright", "Lucida Calligraphy", "Lucida Console",
                "Lucida Fax", "Lucida Handwriting", "Lucida Sans",
                "Lucida Sans Typewriter", "Lucida Sans Unicode",
                "Microsoft Sans Serif", "Monotype Corsiva", "MS Gothic",
                "MS PGothic", "MS Reference Sans Serif", "MS Sans Serif",
                "MS Serif", "Palatino Linotype", "Segoe Print", "Segoe Script",
                "Segoe UI", "Segoe UI Light", "Segoe UI Semibold",
                "Segoe UI Symbol", "Tahoma", "Times", "Times New Roman",
                "Trebuchet MS", "Verdana", "Wingdings", "Wingdings 2",
                "Wingdings 3",
            ],
            "d02f": "124.04347527516074",
        },
        "54ef": ("{\"b_ut\":\"\",\"home_version\":\"V8\",\"in_new_ab\":true,"
                 "\"ab_version\":{\"for_ai_home_version\":\"V8\","
                 "\"in_theme_version\":\"OPEN\",\"enable_web_push\":\"DISABLE\","
                 "\"enable_ai_floor_api\":\"ENABLE\","
                 "\"enable_shortcut_key\":\"DISABLE\","
                 "\"rcmd_timeout_config\":\"550\","
                 "\"home_performance_opt\":\"ssr_fetch_opt\","
                 "\"infra_projection\":\"OFF\"},"
                 "\"ab_split_num\":{\"for_ai_home_version\":54,"
                 "\"in_theme_version\":30,\"enable_web_push\":10,"
                 "\"enable_ai_floor_api\":137,\"enable_shortcut_key\":54,"
                 "\"rcmd_timeout_config\":49,\"home_performance_opt\":49,"
                 "\"infra_projection\":49},"
                 "\"uniq_page_id\":\"1671272756362\",\"is_modern\":true}"),
        "8b94": "",
        "df35": uuid,
        "07a4": "zh-CN",
        "5f45": None,
        "db46": 0,
    }


class BiliApi:
    RISK_CODES = {-352, -412}

    def __init__(self, monitor_cfg=None):
        cfg = monitor_cfg or {}
        self.interval = max(0.5, float(cfg.get("request_interval_seconds", 2.5)))
        self.timeout = float(cfg.get("timeout_seconds", 15))
        self.max_retries = max(1, int(cfg.get("max_retries", 3)))
        self.risk_wait = float(cfg.get("risk_control_wait_seconds", 30))
        self.user_cookie = (cfg.get("cookie") or "").strip()
        self.use_proxy = bool(cfg.get("use_system_proxy", False))
        self.feed_gap = max(4.0, float(cfg.get("feed_page_interval_seconds", 5)))
        if _IMPERSONATE is None:
            log.warning("未安装 curl_cffi, 使用普通 requests (TLS指纹可能触发412, "
                        "建议: pip install curl_cffi)")
        if _IMPERSONATE:
            self.sess = _transport.Session(impersonate=_IMPERSONATE,
                                           trust_env=self.use_proxy)
        else:
            self.sess = _transport.Session()
            self.sess.trust_env = self.use_proxy
        self.sess.headers.update(DEFAULT_HEADERS)
        self._last_req = 0.0
        self._wbi_keys = None
        self._wbi_at = 0.0
        self._arc_ok = True  # arc/search 通道在本进程内是否仍可用
        if self.user_cookie:
            self.sess.headers["Cookie"] = self.user_cookie
        else:
            self._init_guest_identity()

    # ---------- 会话与身份 ----------
    def _set_cookie(self, name, value):
        try:
            self.sess.cookies.set(name, value, domain=".bilibili.com")
        except Exception:
            self.sess.cookies.set(name, value)

    def _init_guest_identity(self):
        """游客身份初始化: 指纹Cookie + 设备指纹上报激活(通过gaia风控)."""
        # 1) buvid 指纹
        try:
            j = self.sess.get(FINGER_SPI_URL, timeout=self.timeout).json()
            d = j.get("data") or {}
            if d.get("b_3"):
                self._set_cookie("buvid3", d["b_3"])
                self._set_cookie("buvid4", d["b_4"])
        except Exception as e:
            log.debug("获取游客指纹失败: %s", e)
        # 2) 主页(补齐 b_nut 等服务端Cookie)
        try:
            self.sess.get(HOME_URL, timeout=self.timeout)
        except Exception as e:
            log.debug("访问主页失败: %s", e)
        # 3) 客户端生成Cookie
        ua = self.sess.headers.get("User-Agent") or DEFAULT_UA
        uuid_ = _gen_uuid()
        self._set_cookie("_uuid", uuid_)
        self._set_cookie("b_lsid", _gen_b_lsid())
        self._set_cookie("buvid_fp", _buvid_fp(ua))
        try:
            if self.sess.cookies.get("b_nut") is None:
                self._set_cookie("b_nut", str(int(time.time())))
        except Exception:
            pass
        # 4) bili_ticket(官方票据, 部分风控场景需要)
        try:
            ts = int(time.time())
            hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(),
                               hashlib.sha256).hexdigest()
            self.sess.get(TICKET_URL, params={
                "key_id": "ec02", "hexsign": hexsign,
                "context[ts]": str(ts), "csrf": "",
            }, timeout=self.timeout)
        except Exception as e:
            log.debug("获取 bili_ticket 失败: %s", e)
        # 5) ExClimbWuzhi 设备指纹上报, 激活 buvid3
        try:
            body = json.dumps({"payload": json.dumps(
                _exclimbwuzhi_payload(ua, uuid_))})
            r = self.sess.post(EXCLIMB_URL, data=body,
                               headers={"Content-Type": "application/json"},
                               timeout=self.timeout)
            log.debug("ExClimbWuzhi 激活: %s", str(r.text)[:80])
        except Exception as e:
            log.debug("ExClimbWuzhi 上报失败: %s", e)

    def _throttle(self):
        jitter = self.interval * random.uniform(0.85, 1.15)
        delta = time.time() - self._last_req
        if delta < jitter:
            time.sleep(jitter - delta)
        self._last_req = time.time()

    def _load_wbi_keys(self):
        if self._wbi_keys and time.time() - self._wbi_at < 3600:
            return self._wbi_keys
        r = self.sess.get(NAV_URL, timeout=self.timeout)
        j = r.json() if r.status_code == 200 else {}
        wbi = ((j.get("data") or {}).get("wbi_img")) or {}
        img_url, sub_url = wbi.get("img_url", ""), wbi.get("sub_url", "")
        if not img_url or not sub_url:
            raise RiskControlError("无法获取 WBI 密钥(nav 接口异常)")
        keys = (img_url.rsplit("/", 1)[-1].split(".")[0],
                sub_url.rsplit("/", 1)[-1].split(".")[0])
        self._wbi_keys, self._wbi_at = keys, time.time()
        return keys

    def _wbi_sign(self, params):
        img_key, sub_key = self._load_wbi_keys()
        mixin = _mixin_key(img_key + sub_key)
        p = {k: FILTED_CHARS.sub("", str(v)) for k, v in params.items()}
        p["wts"] = str(int(time.time()))
        p = dict(sorted(p.items()))
        query = urllib.parse.urlencode(p)
        p["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return p

    # ---------- 请求核心 ----------
    def get_json(self, url, params=None, sign=False, headers=None):
        """带限速/重试/风控退避的 GET; 成功返回 data 字段."""
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                req_params = self._wbi_sign(dict(params or {})) if sign else params
                r = self.sess.get(url, params=req_params, headers=headers,
                                  timeout=self.timeout)
                if r.status_code in (412, 429):
                    raise RiskControlError(f"HTTP {r.status_code}", fatal=True)
                r.raise_for_status()
                j = r.json()
                code = j.get("code", 0)
                if code == 0:
                    return j.get("data") or {}
                if code in self.RISK_CODES:
                    raise RiskControlError(f"code={code} {j.get('message')}")
                raise ApiError(code, j.get("message") or "")
            except RiskControlError as e:
                last_err = e
                log.warning("触发风控(%s), 第%d次冷却重建身份重试", e, attempt)
                self._wbi_keys = None
                if e.fatal:
                    break  # WAF层拦截(412/429)重试无意义, 直接失败
                if not self.user_cookie:
                    time.sleep(self.risk_wait + random.uniform(5, 15))
                    self._init_guest_identity()
                else:
                    time.sleep(self.risk_wait)
            except ApiError:
                raise
            except Exception as e:  # 网络层错误
                last_err = e
                time.sleep(min(2 ** attempt * 2, 30) + random.uniform(0, 2))
        raise last_err

    # ---------- 业务接口 ----------
    def account_card(self, mid):
        """用户名片: data.card.name 昵称, data.follower 粉丝数."""
        return self.get_json(CARD_URL, params={"mid": mid, "photo": "true"})

    def list_videos(self, mid, stop_fn=None, max_pages=500):
        """按发布时间倒序拉取用户投稿; stop_fn(item)->True 时停止(不含该条)."""
        items, pn, total = [], 1, 0
        while True:
            params = {"mid": mid, "pn": pn, "ps": 50, "tid": 0,
                      "keyword": "", "order": "pubdate", "order_avoided": "true"}
            params.update(DM_IMG_PARAMS)
            data = self.get_json(
                ARC_SEARCH_URL, params=params, sign=True,
                headers={"Referer": f"https://space.bilibili.com/{mid}/video"})
            vlist = ((data.get("list") or {}).get("vlist")) or []
            total = int((data.get("page") or {}).get("count") or total)
            if not vlist:
                break
            stop = False
            for it in vlist:
                if stop_fn and stop_fn(it):
                    stop = True
                    break
                items.append(it)
            if stop or (total and len(items) >= total) or pn >= max_pages:
                break
            pn += 1
        return items, total

    @staticmethod
    def _norm_arc(it):
        """arc/search 的 vlist 条目 -> 统一格式."""
        dur = it.get("duration")
        if not isinstance(dur, int) or dur < 0:
            dur = parse_len_seconds(it.get("length"))
        return {
            "bvid": it.get("bvid"),
            "title": _strip_html(it.get("title")),
            "description": _strip_html(it.get("description")),
            "created": it.get("created") if isinstance(it.get("created"), int) else None,
            "length": it.get("length"),
            "duration": dur,
            "pic": it.get("pic"),
            "dyn_id": None,
            "author": it.get("author"),
            "play": it.get("play") if isinstance(it.get("play"), int) else None,
            "comment": it.get("comment") if isinstance(it.get("comment"), int) else None,
        }

    def feed_space_page(self, mid, offset=None):
        """动态流单页(注意: 该接口对请求频率敏感)."""
        params = {"host_mid": mid}
        if offset:
            params["offset"] = offset
        return self.get_json(FEED_SPACE_URL, params=params,
                             headers={"Referer": f"https://space.bilibili.com/{mid}/dynamic"})

    def list_videos_via_feed(self, mid, stop_fn=None, max_pages=3000,
                             start_offset=None):
        """通过动态流按时间倒序枚举视频投稿(arc/search 被风控时的备选通道).

        返回 dict: {items, cursor, exhausted}
        - cursor: 本次翻到的最深处游标(因限流/页数上限中断时), 供下次续读;
        - exhausted: 是否自然翻完(动态流到底);
        - 动态流不含发布时间戳与播放量, 由调用方经 view 接口补齐.
        """
        items, offset, page = [], start_offset, 0
        author, cursor, exhausted = None, None, False
        while page < max_pages:
            page += 1
            if page > 1:
                time.sleep(self.feed_gap * random.uniform(0.9, 1.3))
            data = self.feed_space_page(mid, offset)
            raw = data.get("items") or []
            if not raw:
                # 空页通常是软限流: 递增退避后重试, 最多3次
                for backoff in (10, 20, 40):
                    time.sleep(backoff + random.uniform(0, 5))
                    data = self.feed_space_page(mid, offset)
                    raw = data.get("items") or []
                    if raw:
                        break
                if not raw:
                    log.warning("[mid=%s] 动态流第%d页连续为空, 停止翻页(游标已保存)",
                                mid, page)
                    break
            stop = False
            for it in raw:
                mods = it.get("modules") or {}
                if not isinstance(mods, dict):
                    continue
                if author is None:
                    author = (mods.get("module_author") or {}).get("name")
                md = mods.get("module_dynamic") or {}
                arc = ((md.get("major") or {}).get("archive")) or {}
                if not arc.get("bvid"):
                    continue
                item = {
                    "bvid": arc["bvid"],
                    "title": _strip_html(arc.get("title")),
                    "description": _strip_html(arc.get("desc")),
                    "created": None,
                    "length": arc.get("duration_text"),
                    "duration": parse_len_seconds(arc.get("duration_text")),
                    "pic": arc.get("cover"),
                    "dyn_id": it.get("id_str") or "",
                    "author": author,
                    "play": None,
                    "comment": None,
                }
                if stop_fn and stop_fn(item):
                    stop = True
                    break
                items.append(item)
            offset = data.get("offset")
            if stop:
                break
            if not data.get("has_more") or not offset:
                exhausted = True
                break
            cursor = offset  # 记录最深处游标
        return {"items": items, "cursor": cursor, "exhausted": exhausted}

    def list_videos_auto(self, mid, stop_fn=None, max_pages=500, mode="auto",
                         deep=False, start_offset=None):
        """投稿清单统一入口: 优先 arc/search, 触发风控自动切换动态流.

        返回 dict: {items, total, channel, cursor, exhausted}
        - deep/start_offset 仅作用于动态流通道的深度回填(游标续读).
        """
        if mode in ("auto", "arc") and self._arc_ok:
            try:
                raw, total = self.list_videos(mid, stop_fn=None if deep else stop_fn,
                                              max_pages=max_pages)
                return {"items": [self._norm_arc(it) for it in raw],
                        "total": total, "channel": "arc",
                        "cursor": None, "exhausted": True}
            except RiskControlError as e:
                self._arc_ok = False
                log.warning("[mid=%s] arc/search 通道被风控(%s), 切换动态流通道", mid, e)
                if mode == "arc":
                    raise
                # 冷却: 风控重试已消耗会话信誉, 先歇一会儿再用动态流
                time.sleep(20 + random.uniform(0, 10))
        # 动态流通道: 常规为增量翻页(stop_fn 生效), deep 为全量深度翻页(游标续读)
        fn_stop = None if deep else stop_fn
        res = self.list_videos_via_feed(mid, stop_fn=fn_stop, start_offset=start_offset)
        res["total"], res["channel"] = len(res["items"]), "feed"
        if not res["items"] and not deep:
            raise RiskControlError(f"[mid={mid}] 动态流通道也未获取到数据(疑似限流)")
        return res

    def video_detail(self, bvid):
        """单个视频详情: stat 含播放/点赞/投币/收藏/弹幕等."""
        return self.get_json(VIEW_URL, params={"bvid": bvid})

    def search_users(self, keyword, pages=1):
        """搜索B站用户, 用于查找官号 mid (find 命令)."""
        out = []
        for pn in range(1, max(1, pages) + 1):
            try:
                data = self.get_json(USER_SEARCH_URL, params={
                    "search_type": "bili_user", "keyword": keyword,
                    "pn": pn, "ps": 30,
                }, sign=True)
            except ApiError as e:
                log.warning("用户搜索第%d页失败: %s", pn, e)
                break
            result = data.get("result") or []
            for u in result:
                out.append({
                    "mid": u.get("mid"),
                    "uname": _strip_html(u.get("uname")),
                    "fans": u.get("fans"),
                    "official": _strip_html((u.get("official") or {}).get("title")),
                    "sign": _strip_html(u.get("usign"))[:50],
                })
            if not result:
                break
        return out
