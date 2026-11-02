"""wxauto_linux: 微信 Linux 客户端原生自动化驱动。

提供与 wxautox4.WeChat 兼容的接口子集 (wxauto-mcp 所需), 实现:
- 窗口管理: KWin D-Bus (激活/几何)
- 输入注入: wdotool (libei + xdg-desktop-portal 授权)
- 读取: 全屏截图裁剪 + tesseract OCR (chi_sim)
- 剪贴板: wl-copy (中文粘贴)

可靠性设计 (真实使用踩坑后加固):
- 单操作截止时间 (OP_TIMEOUT): 任何公开操作保证在截止时间内返回明确错误,
  不会卡到 MCP 客户端 30s 超时; 客户端超时后驱动仍在跑的残留操作由
  操作互斥锁 + 忙检测兜住, 后续调用快速失败而不是排队等死。
- 截图/OCR/几何多级缓存: 一次操作内复用同一张截图, 读取类操作 ~3s。
- 状态自恢复: 左侧栏停留在联系人页/搜索页时, Escape + 点聊天图标回到列表。
- 标题锚点按词块聚合 (psm3 常把标题拆成单字), 各处名称出口统一清洗
  OCR 幻觉引号/标点。
"""
import re
import threading
import time
from functools import wraps
from pathlib import Path

from . import winman, wdt, ocr


class WxResponse:
    def __init__(self, success=True, message=''):
        self.success = success
        self.message = message

    def __repr__(self):
        return f'<WxResponse success={self.success} message={self.message!r}>'


class WeChatError(Exception):
    pass


class _OpTimeout(WeChatError):
    pass


class Session:
    """会话对象 (wxautox4 兼容: 属性访问经 .info 字典)。"""

    def __init__(self, info):
        self.info = info

    def __repr__(self):
        return f'<Session {self.info.get("name")!r}>'


# 会话行内的未读红点颜色 (WeChat 深色主题红色徽标)
RED = (237, 92, 75)

# 单操作截止时间: MCP 宿主 (如 ZCode) 通常 30s 超时, 留余量保证错误能返回
OP_TIMEOUT = 24.0
# 截图/OCR 结果复用窗口 (同一次操作内的多个步骤共享一张截图)
CACHE_TTL = 1.5
# 窗口几何复用窗口 (我们自己的操作不会移动窗口)
GEO_TTL = 3.0
# 操作互斥锁等待: 上一操作仍在后台执行时最多等这么久, 然后报忙
BUSY_WAIT = 6.0

# 名称边缘噪声: OCR 幻觉引号/括号/标点 (用户实测出过 '"文件传输助手')
_STRIP_CHARS = (' \t\u3000'
                '"' '\u201c' '\u201d' "'" '\u2018' '\u2019'
                '\u300c' '\u300d' '\u300e' '\u300f'
                '【' '】' '〔' '〕' '(' ')' '（' '）' '[' ']' '<' '>' '《' '》'
                '。' '、' ',' '，' '.' ';' '；' ':' '：' '!' '！' '?' '？'
                '·' '~' '*' '×' '\u2715' '—' '－' '-' '|' '/' '\\')


def _clean_name(s: str) -> str:
    """去掉名称两端的 OCR 幻觉标点/引号 (中间内容保留)。"""
    return (s or '').strip(_STRIP_CHARS)


# 标题行上的单字图标幻觉 (len==1 且在黑名单 → 丢弃)
_TITLE_NOISE = {'—', '一', '。', '·', '口', '口区', '回', '×', '区', '全',
                '日', '吗', 'Q', 'X', 'L', 'O', '曲', '”', '"', '|', '/', '\\',
                '口。', '”全'}

# 左侧导航栏图标位置 (窗口相对比例; 实测聊天 ~0.21H, 联系人 ~0.30H)
NAV_X = 0.035
NAV_CHAT_Y = 0.21
NAV_CONTACT_Y = 0.30


def _serialized(func):
    """公开操作串行化 + 截止时间: 防止客户端超时后残留操作与新操作互相踩。"""
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self._op_lock.acquire(timeout=BUSY_WAIT):
            raise WeChatError(
                f'忙: 上一微信操作仍在执行 (上一次调用可能已客户端超时), '
                f'请等待几秒后重试; 可先调用 wechat_status 查看')
        saved_deadline = self._deadline      # 嵌套调用 (SendMsg→ChatWith) 保留外层
        self._deadline = time.monotonic() + OP_TIMEOUT
        try:
            return func(self, *args, **kwargs)
        except _OpTimeout:
            self._abort_cleanup()
            raise WeChatError(
                f'{func.__name__} 超时({OP_TIMEOUT:.0f}s)已中止: 界面可能不在预期状态, '
                f'可重试或先调用 get_chat_info/get_sessions 观察当前状态')
        finally:
            self._deadline = saved_deadline
            self._op_lock.release()
    return wrapper


class WeChat:
    # 跨实例互斥: 同一时刻只允许一个界面操作 (libei/KWin 都不是线程安全的)
    _op_lock = threading.RLock()

    def __init__(self, nickname=None, start_listener=False, debug=False,
                 resize=True, version='微信', **kwargs):
        if wdt.search() is None:
            # 进程在但窗口被关进托盘 → 尝试恢复
            winman.restore_wechat_from_tray()
            if wdt.search() is None:
                raise WeChatError('微信 Linux 客户端未运行')
        self._last_chat = None
        self._snap = None          # (t, img, rows, geo, scale); rows 可为 None
        self._geo_cache = None     # (t, geo)
        self._deadline = None

    # ---- 截止时间 / 缓存 -------------------------------------------------
    def _tick(self):
        if self._deadline and time.monotonic() > self._deadline:
            raise _OpTimeout()

    def _dismiss_overlay(self):
        """关闭搜索下拉/取消聚焦: 点聊天输入框区域。

        绝对不能用 Escape —— 实测微信 Linux 里 Escape 会把整个窗口藏进
        托盘 (本进程所有 '窗口消失/读数为空' 事故的元凶)。
        """
        try:
            geo = self._cached_geo()
            wdt.click(geo['x'] + geo['w'] * 0.5, geo['y'] + geo['h'] * 0.88)
            time.sleep(0.3)
            self._snap = None
        except Exception:
            pass

    def _abort_cleanup(self):
        """超时中止时尽力把界面恢复干净 (关掉可能打开的搜索/下拉)。"""
        self._dismiss_overlay()

    def invalidate(self):
        self._snap = None

    def _cached_geo(self, refresh=False):
        now = time.monotonic()
        if not refresh and self._geo_cache and now - self._geo_cache[0] < GEO_TTL:
            return self._geo_cache[1]
        geo = winman.get_wechat_geometry()
        if geo is None:
            # 窗口可能被关进托盘 → 恢复并重试 (恢复有时一次不成)
            for _ in range(2):
                try:
                    winman.restore_wechat_from_tray()
                except Exception:
                    pass
                geo = winman.get_wechat_geometry()
                if geo is not None:
                    time.sleep(0.8)      # 窗口刚弹出还在渲染, 等一拍再读
                    break
                time.sleep(0.6)
        if geo is None:
            raise WeChatError('微信窗口不存在 (可能已退出或最小化到托盘且托盘图标不可达), '
                              '请手动打开微信主窗口后重试')
        self._geo_cache = (now, geo)
        return geo

    def _geo(self, retries=3):
        return self._cached_geo()

    def _shot(self, activate=True, refresh=False):
        """窗口截图 (带 TTL 缓存)。返回 (img, geo, scale)。"""
        now = time.monotonic()
        if not refresh and self._snap and now - self._snap[0] < CACHE_TTL:
            return self._snap[1], self._snap[3], self._snap[4]
        if activate:
            geo, activated = winman.activate_and_geo()
            if geo is None:
                raise WeChatError('微信窗口不存在')
            self._geo_cache = (now, geo)
            time.sleep(0.35 if activated else 0.12)
        else:
            geo = self._cached_geo()
        img, geo, scale = ocr.window_shot(wait_activate=False, geo=geo)
        self._snap = (time.monotonic(), img, None, geo, scale)
        return img, geo, scale

    def _window_rows(self, activate=True, refresh=False, psm=3):
        """截图 + 全窗口 OCR (带缓存)。返回 (img, rows, geo, scale)。"""
        now = time.monotonic()
        if not refresh and self._snap and now - self._snap[0] < CACHE_TTL:
            _, img, rows, geo, scale = self._snap
            if rows is None:
                rows = ocr.ocr(img, psm=psm)
                self._snap = (self._snap[0], img, rows, geo, scale)
            return img, rows, geo, scale
        img, geo, scale = self._shot(activate=activate, refresh=True)
        rows = self._to_window_rows(ocr.ocr(img, psm=psm), geo, scale)
        self._snap = (time.monotonic(), img, rows, geo, scale)
        return img, rows, geo, scale

    def _ocr_region(self, img, geo, scale, x0f, y0f, x1f, y1f, psm=3):
        """OCR 窗口内比例子区域, 返回窗口逻辑坐标行 (裁剪 OCR 比全窗口快数倍)。"""
        W, H = geo['w'], geo['h']
        box = (int(x0f * W * scale), int(y0f * H * scale),
               int(x1f * W * scale), int(y1f * H * scale))
        crop = img.crop(box)
        rows = ocr.ocr(crop, psm=psm)
        for r in rows:
            r['lx'] = geo['x'] + x0f * W + r['x'] / scale
            r['ly'] = geo['y'] + y0f * H + r['y'] / scale
            r['lw'] = r['w'] / scale
            r['lh'] = r['h'] / scale
        return rows

    def _state(self, activate=True, refresh=False):
        """读一次窗口状态: (img, rows, geo, scale, anchors)。

        psm3 偶发整块丢失窗口顶部 (搜索框/标题区) → 此时裁顶部条带用
        psm6 补一次 (实测空状态截图 psm3 顶部 0 行, psm6 正常)。
        """
        img, rows, geo, scale = self._window_rows(activate=activate, refresh=refresh)
        a = self._anchors(rows, geo, img, scale)
        if 'search' not in a:
            # psm3 在「无选中聊天」状态下会整块丢掉窗口顶部 → 全图 psm6 补一次
            # (裁条带会破坏 tesseract 版面分析, 必须全图)
            try:
                rows = rows + self._to_window_rows(ocr.ocr(img, psm=6), geo, scale)
                a = self._anchors(rows, geo, img, scale)
            except Exception:
                pass
        return img, rows, geo, scale, a

    # ---- 基础 ----------------------------------------------------------
    def _ensure_main_window(self):
        g = self._cached_geo()
        # 登录窗口是 280x380 小窗; 主窗口明显更大
        if g['w'] < 500:
            raise WeChatError('微信尚未登录 (显示登录窗口), 请先扫码/点击登录')

    def IsOnline(self) -> bool:
        try:
            return wdt.search() is not None
        except Exception:
            return False

    def GetMyInfo(self) -> dict:
        # 界面不直接暴露; 从聊天头/会话无法稳定取得, 返回窗口标题信息
        return {'微信名': 'Linux WeChat', '微信号': '-'}

    # ---- OCR 辅助 -------------------------------------------------------
    @staticmethod
    def _to_window_rows(rows, geo, scale):
        """OCR 图像内坐标 → 窗口逻辑坐标 (相对桌面)。"""
        for r in rows:
            r['lx'] = geo['x'] + r['x'] / scale
            r['ly'] = geo['y'] + r['y'] / scale
            r['lw'] = r['w'] / scale
            r['lh'] = r['h'] / scale
        return rows

    @staticmethod
    def _region_rows(rows, x0f, x1f, y0f, y1f, geo):
        """取窗口内比例区域的 OCR 行 (窗口相对坐标)。"""
        X0, X1 = geo['x'] + geo['w'] * x0f, geo['x'] + geo['w'] * x1f
        Y0, Y1 = geo['y'] + geo['h'] * y0f, geo['y'] + geo['h'] * y1f
        return [r for r in rows if X0 <= r['lx'] <= X1 and Y0 <= r['ly'] <= Y1]

    @staticmethod
    def _tok_ok(r):
        t = r['text'].strip()
        if t in _TITLE_NOISE:
            return False
        return r['conf'] >= (20 if len(t) >= 2 else 20)

    def _anchors(self, rows, geo, img=None, scale=1.0):
        """定位界面锚点: 搜索框与聊天标题 (同一行)。

        标题按词块聚合: psm3 常把标题拆成单字 token ('微'/'信'/'团'/'队'),
        旧版要求单 token len>=2 导致锚点丢失 → GetSession 永远为空。
        标题区左界优先用绿色高亮行的右缘 (搜索框右侧 ⊕ 按钮会被 OCR 幻觉成
        '曲'/'。' 等单字), 无高亮时退化用 conf 门限 + 噪声黑名单。
        """
        a = {}
        for r in rows:
            if r['text'].strip() == '搜索' and r['ly'] < geo['y'] + geo['h'] * 0.15:
                a['search'] = r
                break
        if 'search' not in a:
            return a
        sy = a['search']['ly']
        sx = a['search']['lx']
        rect = self._selected_session_rect(img, geo, scale) if img is not None else None
        if rect is not None and rect[2] > sx:
            xgate = rect[2] + 4
        else:
            xgate = sx + geo['w'] * 0.03
        toks = [r for r in rows
                if r['lx'] > xgate
                and abs(r['ly'] - sy) < 25
                and self._tok_ok(r)]
        toks.sort(key=lambda r: r['lx'])
        if toks:
            name = _clean_name(self._join([t['text'] for t in toks]))
            if len(name) >= 2:
                a['title'] = toks[0]
                a['title_all'] = toks
                a['title_name'] = name
        return a

    def _title_name(self, a, img=None, geo=None, scale=1.0):
        """当前聊天标题: 锚点词块 → 标题条带 psm6 兜底。"""
        if a.get('title_name'):
            return a['title_name']
        s = a.get('search')
        if s is not None and img is not None:
            rx = (s['lx'] - geo['x']) / geo['w']
            ry = (s['ly'] - geo['y']) / geo['h']
            try:
                strip = self._ocr_region(img, geo, scale,
                                         max(0.0, rx + 0.20), max(0.0, ry - 0.02),
                                         1.0, min(1.0, (ry * geo['h'] + 48) / geo['h']),
                                         psm=6)
                # 只保留标题行 (与搜索框同行) 的词块, 排除下方的会话行
                strip = [r for r in strip if abs(r['ly'] - s['ly']) <= 28]
                strip.sort(key=lambda r: r['lx'])
                name = _clean_name(self._join([t['text'] for t in strip
                                               if self._tok_ok(t)]))
                if len(name) >= 2:
                    return name
            except Exception:
                pass
        return ''

    @staticmethod
    def _cluster_lines(items):
        """OCR 词块按 y 聚成视觉行; 容差随字号自适应。返回按 lx 排序的行列表。"""
        items = sorted(items, key=lambda r: r['ly'])
        lines = []
        for r in items:
            tol = max(10, (lines[-1][0].get('lh', 14) if lines else 14) * 0.7)
            if lines and abs(r['ly'] - lines[-1][0]['ly']) < tol:
                lines[-1].append(r)
            else:
                lines.append([r])
        return [sorted(l, key=lambda r: r['lx']) for l in lines]

    @staticmethod
    def _join(tokens):
        """把 OCR 词块拼成可读文本: 中文间不加空格, ASCII 词间加空格。"""
        out = ''
        for t in tokens:
            t = t.strip()
            if not t:
                continue
            if out and out[-1].isascii() and out[-1].isalnum() and t[0].isascii() and t[0].isalnum():
                out += ' '
            out += t
        return out

    TIME_RE = re.compile(r'^(\d{1,2}):?(\d{2})$|^星期.|\d{1,2}月\d{1,2}日|^昨天$')

    # 时间列常见 OCR 误读: '星期日'→'显期日', '02:21'→'0221'/'02221'/'008'
    _TIME_TOK_RE = re.compile(
        r'^[^\w\u4e00-\u9fff]{0,2}(?:\d{1,2}[:.：]\d{2}|\d{3,6}|.{0,2}期[日一二三四五六天]|昨天|前天)[^\w\u4e00-\u9fff]{0,2}$')

    @classmethod
    def _looks_like_time(cls, t: str) -> bool:
        return bool(cls._TIME_TOK_RE.match((t or '').strip()))

    @staticmethod
    def _norm_time(t: str) -> str:
        t = _clean_name(t or '')
        if re.match(r'^\d{4,6}$', t):          # '0221'/'02221' → '02:21'
            return f'{int(t[:2]) % 24:02d}:{t[-2:]}'
        m = re.match(r'^(\d{1,2})[:.：](\d{2})$', t)
        if m:
            return f'{int(m.group(1)):02d}:{m.group(2)}'
        mw = re.search(r'期([日一二三四五六天])', t)
        if mw:
            return f'星期{mw.group(1)}'
        return t

    # ---- 名称匹配 -------------------------------------------------------
    @staticmethod
    def _name_similar(a: str, b: str) -> bool:
        """OCR 噪声容忍的名称相似度: 字符集重合率 ≥ 0.7。"""
        sa, sb = set(a or ''), set(b or '')
        if not sa or not sb:
            return False
        return len(sa & sb) / max(len(sa), len(sb)) >= 0.7

    @staticmethod
    def _name_match(a: str, b: str) -> bool:
        """名称匹配: 严格字符集 ≥0.7; 双方 ≥4 字时放宽为 共享字≥2 且长度差≤1。

        放宽是为了扛首字 OCR 污染 (如 '张星星' 被读成 ',从星星');
        短名 (<4字) 不放宽, 避免把 '测试' 误配到 '测试群'。
        """
        sa, sb = set(a or ''), set(b or '')
        if not sa or not sb:
            return False
        if len(sa & sb) / max(len(sa), len(sb)) >= 0.7:
            return True
        if len(a) >= 4 and len(b) >= 4 and abs(len(a) - len(b)) <= 1:
            return len(sa & sb) >= 2
        return False

    def _line_point(self, line):
        """一行 OCR 词块的点击点: 行内水平中点、首词块垂直中心。"""
        first, last = line[0], line[-1]
        return ((first['lx'] + last['lx'] + last['lw']) / 2,
                first['ly'] + first['lh'] / 2)

    def _has_red_dot(self, img, row, geo, scale, x_range):
        import numpy as np
        try:
            a = np.asarray(img.convert('RGB'))
        except Exception:
            return False
        sx = winman.desktop_logical_size().get('scale', 1.0)
        x0 = int((geo['x'] + geo['w'] * x_range[0]) * sx)
        x1 = int((geo['x'] + geo['w'] * x_range[1]) * sx)
        y0 = int(row['ly'] * sx)
        y1 = int((row['ly'] + max(row['lh'], 14)) * sx)
        region = a[max(0, y0):y1, max(0, x0):x1]
        if region.size == 0:
            return False
        r, g, b = region[..., 0].astype(int), region[..., 1].astype(int), region[..., 2].astype(int)
        return bool(((r > 190) & (g < 130) & (b < 130)).sum() > 12)

    # ---- 状态恢复 -------------------------------------------------------
    def _recover_main_page(self):
        """回到主界面 (聊天列表): 聚焦搜索框 → 清空关键词 → 点聊天图标。

        覆盖三种残留状态: 搜索下拉张开 / 全窗网络结果页 (无导航栏) /
        左侧栏在联系人页。搜索框位置固定 (左栏顶部), 清空关键词后
        微信自动退出搜索态。绝不能用 Escape —— 会把窗口藏进托盘。
        """
        try:
            geo = self._cached_geo()
            wdt.click(geo['x'] + geo['w'] * 0.12, geo['y'] + geo['h'] * 0.078)
            time.sleep(0.4)
            wdt.key_ctrl_a()
            time.sleep(0.12)
            wdt.key_delete()
            time.sleep(0.5)
            wdt.click(geo['x'] + geo['w'] * NAV_X, geo['y'] + geo['h'] * NAV_CHAT_Y)
            time.sleep(0.6)
            self._snap = None
        except Exception:
            pass

    # ---- 会话列表 -------------------------------------------------------
    def _session_rows(self, activate=True, retries=1, refresh=False, recover=True):
        """解析会话列表, 返回 (entries(含 _y), img, rows, geo, scale, anchors)。

        只强制要求搜索框锚点 (标题找不到时用绿色高亮行/搜索框宽度推边界);
        找不到搜索框时尝试状态恢复后重读。
        """
        img = rows = geo = scale = a = None
        for attempt in range(retries + 1):
            self._tick()
            img, rows, geo, scale, a = self._state(activate=activate,
                                                   refresh=refresh or attempt > 0)
            if 'search' in a:
                break
            time.sleep(0.5)
        if 'search' not in a:
            if recover:
                self._recover_main_page()
                img, rows, geo, scale, a = self._state(activate=True, refresh=True)
            if 'search' not in a:
                return [], img, rows, geo, scale, a
        sx, sy = a['search']['lx'], a['search']['ly']
        W, H = geo['w'], geo['h']
        # 面板右界: 标题锚点 > 绿色高亮行 > 搜索框宽度推算
        # 面板右界: 绿色高亮行 > 标题锚点 > 搜索框宽度推算
        # (绿色高亮行横跨整个会话面板, 比标题位置更可靠)
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None and rect[2] > sx:
            x1 = rect[2] - W * 0.004
        elif 'title' in a:
            x1 = a['title']['lx'] - W * 0.008
        else:
            x1 = sx + W * 0.27
        x0 = sx - W * 0.03
        y0 = sy + a['search']['lh'] * 1.8          # 搜索栏以下
        items = [r for r in rows if x0 <= r['lx'] <= x1 and r['ly'] >= y0]
        lines = self._cluster_lines(items)
        timex0 = x1 - W * 0.065                    # 时间列起始 x
        icontimex = x1 - W * 0.025                 # 静音铃铛图标列
        avx0, avx1 = sx - W * 0.06, sx             # 头像列 (红点检测)
        sessions = []
        cur = None
        for line in lines:
            texts = list(line)
            joined = self._join([t['text'] for t in texts])
            if not joined:
                continue
            head_time = ''
            head_y = 0.0
            rest = texts
            right = [t for t in texts if t['lx'] >= timex0]
            for i, t in enumerate(texts):
                ttx = t['text'].strip()
                if t['lx'] >= timex0 and self._looks_like_time(ttx):
                    head_time = ttx
                    head_y = t['ly']
                    rest = texts[:i] + texts[i + 1:]
                    break
            if not head_time and right:
                # token 被拆散 ('显'+'期'+'日') → 拼整行右侧再判一次
                jt = self._join([t['text'] for t in right])
                if self._looks_like_time(jt):
                    head_time = jt
                    head_y = right[0]['ly']
                    rest = [t for t in texts if t['lx'] < timex0]
            if head_time and rest:
                nm = _clean_name(self._join([t['text'] for t in rest if t['lx'] < icontimex]))
                m = re.search(r'\[(\d+)条\]', nm)
                new_count = int(m.group(1)) if m else 0
                if m:
                    nm = nm.replace(m.group(0), '')
                head_time = self._norm_time(head_time)
                entry = {'name': nm, 'time': head_time, 'content': '',
                         'isnew': new_count > 0, 'new_count': new_count,
                         'ismute': any(t['lx'] >= icontimex for t in rest),
                         '_y': head_y}
                sessions.append(entry)
                cur = entry
            elif cur is not None:
                body = _clean_name(self._join([t['text'] for t in texts if t['lx'] < icontimex]))
                if body:
                    cur['content'] = body
                    if texts and texts[-1]['lx'] >= icontimex:
                        cur['ismute'] = True
                    m2 = re.search(r'\[(\d+)条\]', body)
                    if m2:
                        cur['new_count'] = int(m2.group(1))
                        cur['isnew'] = True
        # 红点检测: 头行左侧头像区找红色像素
        if sessions:
            import numpy as np
            try:
                arr = np.asarray(img.convert('RGB'))
            except Exception:
                arr = None
            for entry in sessions:
                hy = entry['_y']
                region_red = False
                if arr is not None:
                    px0 = int((avx0 - geo['x']) * scale)
                    px1 = int((avx1 - geo['x']) * scale)
                    py0 = int((hy - geo['y'] - 18) * scale)
                    py1 = int((hy - geo['y'] + 26) * scale)
                    reg = arr[max(0, py0):max(0, py1), max(0, px0):max(0, px1)]
                    if reg.size:
                        rr, gg, bb = reg[..., 0].astype(int), reg[..., 1].astype(int), reg[..., 2].astype(int)
                        region_red = bool(((rr > 190) & (gg < 130) & (bb < 130)).sum() > 12)
                if region_red:
                    entry['isnew'] = True
                    entry['new_count'] = entry['new_count'] or 1
        # 选中行 (绿色高亮) 在全窗口 psm3 下常整行丢失 → 条带 OCR 合成补齐
        if rect is not None:
            near = [e for e in sessions if abs(e['_y'] - rect[0]) < 26]
            if not near:
                nm = self._green_row_read(img, geo, scale, rect)
                if nm:
                    entry = {'name': nm, 'time': '', 'content': '',
                             'isnew': False, 'new_count': 0, 'ismute': False,
                             '_y': rect[0], 'is_selected': True}
                    sessions.append(entry)
                    sessions.sort(key=lambda e: e['_y'])
        return sessions, img, rows, geo, scale, a

    def _selected_session_rect(self, img, geo, scale):
        """绿色高亮行 (当前选中会话) 的窗口逻辑 (y, x0, x1); 无高亮返回 None。"""
        if img is None:
            return None
        import numpy as np
        try:
            arr = np.asarray(img.convert('RGB'))
        except Exception:
            return None
        r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
        mask = (g > 110) & (g - r > 30) & (g - b > 30)
        Wpx = arr.shape[1]
        # 只看左侧会话面板 (窗口宽 42% 以内): 聊天区绿色气泡也命中绿色,
        # 不裁掉会把面板右界推到气泡上, 整个会话解析崩掉
        panel_w = int(Wpx * 0.42)
        mask[:, panel_w:] = False
        counts = mask.sum(axis=1)
        ys = np.where(counts > panel_w * 0.4)[0]
        if len(ys) == 0:
            return None
        y_med = float(np.median(ys))
        band = mask[max(0, int(y_med) - 30):int(y_med) + 30]
        cols = np.where(band.sum(axis=0) > 10)[0]
        if len(cols) == 0:
            return None
        rect = (geo['y'] + y_med / scale,
                geo['x'] + float(cols.min()) / scale,
                geo['x'] + float(cols.max()) / scale)
        if rect[2] - rect[1] < geo['w'] * 0.15:    # 退化 (太窄不可信)
            return None
        return rect

    def _green_bubbles(self, img, geo, scale, x_min_logical):
        """聊天区绿色气泡 (自己发的消息: 深色字+绿底) 检测与 OCR。

        psm3/psm6 对绿底深字的小气泡经常整块失读; 这里先找绿色气泡框,
        再框内找深色文字 bbox, 裁紧后二值化 psm6 —— 实测 '测试' 与
        长文本气泡都能稳定读出。返回 [{ly, lx, lxr, text}]。
        """
        import numpy as np
        try:
            arr = np.asarray(img.convert('RGB'))
            gray = np.asarray(img.convert('L'))
        except Exception:
            return []
        r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
        mask = (g > 150) & (g - r > 60) & (g - b > 40)
        px_panel = int((x_min_logical - geo['x']) * scale)
        mask[:, :max(0, px_panel)] = False       # 排除会话面板 (选中行/图标)
        Hpx = arr.shape[0]
        ys = np.where(mask.sum(axis=1) > 30)[0]
        segs = []
        for y in ys:
            if segs and y - segs[-1][1] <= 6:
                segs[-1][1] = y
            else:
                segs.append([y, y])
        out = []
        for y0, y1 in segs:
            if y1 - y0 < 14:                     # 图标碎片
                continue
            band = mask[y0:y1 + 1]
            colok = band.sum(axis=0) > 2
            xs = np.where(colok)[0]
            if len(xs) == 0 or xs.max() - xs.min() < 24:
                continue
            x0, x1 = int(xs.min()), int(xs.max())
            sub = gray[y0:y1 + 1, x0:x1 + 1]
            dm = (sub < 115) & colok[x0:x1 + 1][None, :]   # 深色文字像素
            if dm.sum() < 25:
                continue
            dys, dxs = np.where(dm)
            pad = 5
            cy0 = max(0, y0 + int(dys.min()) - pad)
            cy1 = min(Hpx, y0 + int(dys.max()) + pad)
            cx0 = max(0, x0 + int(dxs.min()) - pad)
            cx1 = x0 + int(dxs.max()) + pad
            g2 = gray[cy0:cy1, cx0:cx1]
            bw = np.where(g2 < 115, 0, 255).astype('uint8')
            from PIL import Image as _I
            try:
                txt = ''.join(t['text'] for t in ocr.ocr(_I.fromarray(bw), psm=6)
                              if t['text'].strip())
            except Exception:
                continue
            if not txt:
                continue
            out.append({'ly': geo['y'] + (cy0 + cy1) / 2 / scale,
                        'ly0': geo['y'] + cy0 / scale,
                        'ly1': geo['y'] + cy1 / scale,
                        'lx': geo['x'] + cx0 / scale,
                        'lxr': geo['x'] + cx1 / scale,
                        'text': txt})
        return out

    def _green_row_read(self, img, geo, scale, rect):
        """绿色选中行的名字条带 OCR (psm7 单行)。

        全窗口 psm3 对绿底白字经常整行丢失; 条带只裁名字行 (绿块上半部,
        跳过头像列、排除时间列与内容预览行), 实测 3/3 稳定读出。
        """
        try:
            W = geo['w']
            x0 = int((rect[1] + 40 - geo['x']) * scale)
            x1 = int((rect[2] - W * 0.075 - geo['x']) * scale)
            y0 = int((rect[0] - geo['y'] - 24) * scale)
            y1 = int((rect[0] - geo['y'] + 3) * scale)
            strip = img.crop((max(0, x0), max(0, y0), x1, min(img.height, y1)))
            rs = ocr.ocr(strip, psm=7)
        except Exception:
            return ''
        toks = [r for r in rs
                if r['text'].strip() and (r['conf'] > 20 or len(r['text'].strip()) > 1)]
        toks.sort(key=lambda r: r['x'])
        return _clean_name(self._join([t['text'] for t in toks]))

    @_serialized
    def GetSession(self) -> list:
        self._ensure_main_window()
        sessions, img, rows, geo, scale, a = self._session_rows()
        return [Session(e) for e in sessions]

    # ---- 当前聊天 -------------------------------------------------------
    def _current_chat(self, refresh=False):
        """当前选中会话名: 绿色高亮行匹配; 退化用标题锚点/标题条带。"""
        try:
            sessions, img, rows, geo, scale, a = self._session_rows(
                activate=False, refresh=refresh, retries=0, recover=False)
        except WeChatError:
            raise
        except Exception:
            geo = self._cached_geo()
            return self._last_chat or ''
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None:
            sel = [e for e in sessions if e.get('is_selected')]
            if sel:
                return sel[0]['name']
            near = [e for e in sessions if abs(e['_y'] - rect[0]) < 26]
            if near and near[0]['name']:
                return near[0]['name']
            nm = self._green_row_read(img, geo, scale, rect)
            if nm:
                return nm
        name = self._title_name(a, img, geo, scale)
        if name:
            return name
        return self._last_chat or ''

    @_serialized
    def ChatInfo(self) -> dict:
        self._ensure_main_window()
        return {'chat_name': self._current_chat()}

    # ---- 聊天切换 / 发送 -------------------------------------------------
    # 搜索下拉的分区头 (整行): 之前的行全是搜索入口, 之后的才是联系人
    SEARCH_SECTIONS = ('功能', '联系人', '聊天记录', '群聊', '朋友圈', '公众号')
    # 搜索入口/动作行的特征词 (如「文件传输助手 打开」)
    SEARCH_ACTION_WORDS = ('打开', '已读', '恢复', '数据', '小程序', '搜索')

    def _search_candidate(self, cand_rows, who, exact):
        """在搜索下拉行里选目标, 返回 (trust, y, (x,y)) 或 None。

        trust: 0=分区头后首行(结构定位, 最可靠) 1=精确 2=模糊 3=子串;
        同级取更靠上的行 —— 网络结果行总在底部。
        """
        lines = self._cluster_lines(cand_rows)
        names = [self._join([t['text'] for t in l]) for l in lines]
        out = []
        skip_rest = False
        for i, name in enumerate(names):
            if not name:
                continue
            if name in self.SEARCH_SECTIONS or name.startswith(('网络搜索', '搜索网络')):
                # 「搜索网络结果」分区的子行点了会进全窗网页 → 整段排除
                skip_rest = name.startswith(('网络搜索', '搜索网络'))
                continue
            if skip_rest:
                continue
            if not skip_rest:
                # 分区头 (功能/联系人/群聊...) 后首个匹配行 = 结构定位 (trust 0)
                j = i + 1
                while j < len(lines) and (not names[j]
                                          or names[j] in self.SEARCH_SECTIONS
                                          or names[j].startswith(('网络搜索', '搜索网络'))):
                    j += 1
                if j < len(lines):
                    nm2 = names[j]
                    if self._name_match(who, nm2) or who in nm2 or nm2 in who:
                        out.append((0, lines[j][0]['ly'], self._line_point(lines[j])))
            continue
            has_action = any(wd in name for wd in self.SEARCH_ACTION_WORDS)
            if has_action:
                continue
            # 聊天记录内容行 ('[聊天记录]xxx' / '...的聊天记录' / '共N条相关聊天记录')
            if name.startswith('[') or '聊天记录]' in name or '条相关' in name:
                continue
            if name == who:
                pri = 1
            elif self._name_match(who, name):
                pri = 2
            elif not exact and (who in name or name in who):
                pri = 3
            else:
                continue
            out.append((pri, lines[i][0]['ly'], self._line_point(lines[i])))
        if not out:
            return None
        # 同级取更靠上的行: 下拉里网络结果行总在最底部, 真实行在分区头下
        out.sort(key=lambda c: (c[0], c[1]))
        return out[0]

    def _dropdown_rows(self, img, geo, scale, s, psms=(3, 6)):
        """搜索框下方下拉区域的裁剪 OCR (比全窗口 OCR 快且准)。"""
        rx = (s['lx'] - geo['x']) / geo['w']
        ry = (s['ly'] - geo['y']) / geo['h']
        x0f = max(0.0, rx - 0.03)
        x1f = min(1.0, rx + 0.30)
        y0f = min(0.85, ry + s['lh'] / geo['h'] + 0.008)
        y1f = 0.90
        out = []
        for psm in psms:
            out.extend(self._ocr_region(img, geo, scale, x0f, y0f, x1f, y1f, psm=psm))
        return out

    @_serialized
    def ChatWith(self, who: str, exact: bool = True, force: bool = False,
                 force_wait: float = 0.5) -> WxResponse:
        self._ensure_main_window()
        winman.activate_wechat()
        self._tick()
        # 0. 快路径: 目标已在会话列表 → 直接点该行 (绕开搜索下拉的不稳定 OCR)
        try:
            sessions, img, rows, geo, scale, a = self._session_rows(
                activate=False, retries=0, recover=False)
        except WeChatError:
            raise
        except Exception:
            sessions, img, rows, geo, scale, a = [], None, [], self._cached_geo(), 1.0, {}
        if 'search' in a and sessions:
            # 已经在目标聊天 → 直接成功。绝不能点已选中的会话行:
            # 实测微信 Linux 里再点一次选中行会把聊天收起 (toggle)
            try:
                cur0 = self._current_chat()
            except Exception:
                cur0 = ''
            if cur0 and self._name_match(who, cur0):
                self._last_chat = who
                return WxResponse(True, f'已在 {who} (无需切换)')
            hit = None
            for e in sessions:
                nm = e.get('name', '')
                if nm and self._name_match(who, nm):
                    hit = e
            if hit is not None:
                self._snap = None
                wdt.click(a['search']['lx'] + geo['w'] * 0.13, hit['_y'] + 5)
                time.sleep(0.7)
                cur = self._current_chat()
                if cur and self._name_match(who, cur):
                    self._last_chat = who
                    return WxResponse(True, f'切换到 {who} (会话列表)')
                self._tick()
        # 1. 搜索: OCR 定位搜索框锚点后点击 (位置随窗口尺寸变化)
        try:
            img, rows, geo, scale, a = self._state(activate=False)
        except Exception:
            img, rows, geo, scale, a = self._state(activate=True, refresh=True)
        if 'search' not in a:
            self._recover_main_page()
            img, rows, geo, scale, a = self._state(activate=True, refresh=True)
            if 'search' not in a:
                return WxResponse(False, '未找到搜索框 (窗口可能不在主界面/未登录)')
        s = a['search']
        self._snap = None
        wdt.click(s['lx'] + s['lw'] / 2, s['ly'] + s['lh'] / 2)
        time.sleep(0.6)
        # 2. 清空并粘贴联系人名
        wdt.key_ctrl_a()
        time.sleep(0.15)
        self._clipboard(who)
        wdt.key_ctrl_v()
        time.sleep(1.2)
        self._tick()
        # 3. 裁剪 OCR 下拉 (psm3+psm6 双通道); 网络搜索结果可能把联系人
        #    顶下去 → 找不到就下滚列表再找一轮
        best = None
        for attempt in range(2):
            img, geo, scale = self._shot(activate=False, refresh=True)
            cand = self._dropdown_rows(img, geo, scale, s)
            c = self._search_candidate(cand, who, exact)
            if c is not None and (best is None
                                  or (c[0], -c[1]) < (best[0], -best[1])):
                best = c
            if best is not None:
                break
            self._tick()
            wdt.mousemove(s['lx'] + geo['w'] * 0.12, s['ly'] + geo['h'] * 0.35)
            wdt.run(['scroll', '--', '0', '400'])
            time.sleep(0.7)
        if best is None:
            self._recover_main_page()
            return WxResponse(False,
                              f'搜索结果中未找到: {who} (下拉 OCR 未匹配到该名称)')
        # 4. 点击后必须验证切换成功 —— 防止点击落空后消息发进别的会话 (安全关键)
        self._snap = None
        wdt.click(best[2][0], best[2][1])
        time.sleep(0.8)
        cur = self._current_chat()
        ok = bool(cur) and self._name_match(who, cur)
        if not ok:
            self._recover_main_page()
            self._last_chat = None
            return WxResponse(False, f'切换验证失败: 当前为 {cur!r}, 已取消 (目标 {who!r})')
        self._last_chat = who
        return WxResponse(True, f'切换到 {who}')

    def _input_point(self, activate=False):
        """定位输入框点击点: 优先「发送」按钮上方, 退化用固定比例。"""
        geo = self._cached_geo()
        try:
            img, rows, g2, scale = self._window_rows(activate=activate)
        except Exception:
            rows = []
            g2 = geo
        send = None
        for r in rows:
            if r['text'].strip() == '发送' and r['ly'] > g2['y'] + g2['h'] * 0.7:
                send = r
                break
        if send is not None:
            return (send['lx'] - g2['w'] * 0.30, send['ly'] - max(send['lh'] * 2.5, 30)), g2
        return (g2['x'] + g2['w'] * 0.6, g2['y'] + g2['h'] * 0.78), g2

    def _verify_target(self, who: str) -> WxResponse | None:
        """发送前二次验证当前聊天确实是 who; 不匹配则拒绝 (防误发)。

        走缓存 (ChatWith 刚验证过的截图), 通常零开销。
        """
        cur = self._current_chat()
        if cur and self._name_match(who, cur):
            return None
        return WxResponse(False, f'安全拦截: 当前聊天 {cur!r} ≠ {who!r}, 未发送')

    @_serialized
    def SendMsg(self, msg: str, who: str = None, clear: bool = True,
                at=None, exact: bool = False, max_retries: int = 3) -> WxResponse:
        self._ensure_main_window()
        if who:
            r = self.ChatWith(who, exact=exact)
            if not r.success:
                return r
            guard = self._verify_target(who)
            if guard is not None:
                return guard
        self._tick()
        winman.activate_wechat()
        self._snap = None
        # 粘贴前硬校验: 窗口必须真实存在且在主界面 (防止窗口消失后盲发到别的应用)
        try:
            img, rows, g3, scale3 = self._window_rows(activate=True, refresh=True)
        except Exception as e:
            return WxResponse(False, f'发送中止: 微信窗口不可用 ({e})')
        if 'search' not in self._anchors(rows, g3, img, scale3):
            return WxResponse(False, '发送中止: 微信窗口不在主界面 (可能未登录或被遮挡)')
        (ix, iy), _ = self._input_point()
        wdt.click(ix, iy)
        time.sleep(0.4)
        self._clipboard(msg)
        wdt.key_ctrl_v()
        time.sleep(0.35)
        wdt.key_enter()
        time.sleep(0.7)
        self._snap = None
        return WxResponse(True, '发送成功')

    @_serialized
    def SendFiles(self, filepath, who=None, exact=False, max_retries=3) -> WxResponse:
        if who:
            r = self.ChatWith(who, exact=exact)
            if not r.success:
                return r
            guard = self._verify_target(who)
            if guard is not None:
                return guard
        paths = filepath if isinstance(filepath, list) else [filepath]
        for p in paths:
            if not Path(p).exists():
                return WxResponse(False, f'文件不存在: {p}')
        self._tick()
        winman.activate_wechat()
        self._snap = None
        try:
            img, rows, g3, scale3 = self._window_rows(activate=True, refresh=True)
        except Exception as e:
            return WxResponse(False, f'发送中止: 微信窗口不可用 ({e})')
        if 'search' not in self._anchors(rows, g3, img, scale3):
            return WxResponse(False, '发送中止: 微信窗口不在主界面 (可能未登录或被遮挡)')
        text = '\n'.join('file://' + str(p) for p in paths)
        (ix, iy), _ = self._input_point()
        wdt.click(ix, iy)
        time.sleep(0.4)
        self._clipboard(text)
        wdt.key_ctrl_v()
        time.sleep(1.2)
        wdt.key_enter()
        time.sleep(1.0)
        self._snap = None
        return WxResponse(True, f'发送 {len(paths)} 个文件')

    # ---- 消息读取 -------------------------------------------------------
    @_serialized
    def GetAllMessage(self) -> list:
        self._ensure_main_window()
        img, rows, geo, scale, a = self._state(activate=True)
        if 'search' not in a:
            # 左侧栏不在聊天页 → 恢复后重读一次
            self._recover_main_page()
            img, rows, geo, scale, a = self._state(activate=True, refresh=True)
            if 'search' not in a:
                return []
        t = a.get('title')
        W, H = geo['w'], geo['h']
        chat_name = self._last_chat or ''
        if t:
            near = [r for r in rows
                    if abs(r['ly'] - t['ly']) < 20
                    and t['lx'] <= r['lx'] <= t['lx'] + t['lw'] + 60 * (W / 855)
                    and self._tok_ok(r)]
            near.sort(key=lambda r: r['lx'])
            chat_name = _clean_name(self._join([r['text'] for r in near])) or chat_name
        if not chat_name:
            chat_name = self._title_name(a, img, geo, scale) or self._last_chat or ''
        x0 = (t['lx'] if t else a['search']['lx'] + W * 0.27) - W * 0.015
        # 若能定位到会话面板右缘 (绿色高亮行), 用它排除会话列泄漏
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None and rect[2] < (t['lx'] if t else x0 + W):
            x0 = rect[2] + W * 0.004
        y0 = (t['ly'] + t['lh'] * 2.2) if t else a['search']['ly'] + a['search']['lh'] * 3
        # 聊天区底界: 输入区高度近似恒定 (~150 逻辑px), 用窗口高推导可避免
        # 把输入框草稿读成消息 (旧版固定 0.86H 在小窗口会把草稿区包进来)
        y1 = geo['y'] + H - max(150, H * 0.13)
        # 自己发的气泡是「深色字+绿底」, psm3 对其系统性失读 → 单独检测 OCR
        gbs = self._green_bubbles(img, geo, scale, x0)
        chat = [r for r in rows if x0 <= r['lx'] and y0 <= r['ly'] <= y1]
        # 落在绿气泡带内的 psm3 行是该气泡的碎片 → 去掉
        def _in_gb(r):
            return any(gb['ly0'] - 4 <= r['ly'] <= gb['ly1'] + 4 for gb in gbs)
        chat = [r for r in chat if not _in_gb(r)]
        # 过滤低置信单字 (二维码/图片区域的 OCR 乱码)
        chat = [r for r in chat if r['conf'] > 40 or len(r['text'].strip()) > 1]
        lines = self._cluster_lines(chat)
        center = x0 + (geo['x'] + W - x0) * 0.5    # 聊天区中线
        # 合并: psm3 行 + 绿气泡 (气泡强制 self), 按 y 排序
        merged = []
        for line in lines:
            joined = self._join([t2['text'] for t2 in line])
            if not joined:
                continue
            first = line[0]
            merged.append({'ly': first['ly'], 'lh': first['lh'], 'lx': first['lx'],
                           'text': joined, 'green': False})
        for gb in gbs:
            merged.append({'ly': gb['ly'], 'lh': 20, 'lx': gb['lx'],
                           'text': gb['text'], 'green': True})
        merged.sort(key=lambda m: m['ly'])
        messages = []
        cur_sender = None
        last_line_y = None
        for m in merged:
            joined = m['text']
            first_ly = m['ly']
            if m['green']:
                side = 'self'
            else:
                side = 'self' if m['lx'] > center + 8 else 'friend'
                # 时间行 (居中小字) / “N条新消息”浮标 → 忽略
                if re.match(r'^\d{1,2}:\d{2}$', joined) and m['lx'] + 20 > center - W * 0.08                         and m['lx'] < center + W * 0.08:
                    continue
                if '条新消息' in joined:
                    continue
            # 行间距很大 → 新气泡, 群聊里可能是新发言人的昵称行
            new_bubble = (last_line_y is None
                          or (first_ly - last_line_y) > m['lh'] * 2.2)
            if new_bubble and side == 'friend':
                cur_sender = None
            is_sender_name = (side == 'friend' and m['lh'] < 16
                              and len(joined) < 24
                              and m['lx'] < x0 + (geo['x'] + W - x0) * 0.18)
            if is_sender_name:
                cur_sender = joined
            else:
                messages.append(Message(
                    content=joined,
                    sender=cur_sender or ('我' if side == 'self' else ''),
                    type='text',
                    attr='self' if side == 'self' else 'friend',
                    chat={'name': chat_name},
                    time='',
                ))
            last_line_y = first_ly
        return messages

    @_serialized
    def GetHistoryMessage(self, n: int = 50) -> list:
        # 向上滚动几次后读取
        geo = self._cached_geo()
        winman.activate_wechat()
        self._snap = None
        time.sleep(0.2)
        mx = geo['x'] + geo['w'] * 0.68
        my = geo['y'] + geo['h'] * 0.4
        wdt.mousemove(mx, my)
        time.sleep(0.2)
        for _ in range(3):
            self._tick()
            wdt.run(['scroll', '--', '0', '600'])
            time.sleep(0.3)
        time.sleep(0.6)
        return self.GetAllMessage()

    # ---- 联系人 ---------------------------------------------------------
    # 联系人页分区头 (不算好友)
    _CONTACT_HEADERS = {'新的朋友', '仅聊天的朋友', '群聊', '标签', '公众号',
                        '公众号、服务号', '企业微信联系人', '通讯录管理',
                        '联系人', '朋友', '网络联系人'}

    def _has_avatar(self, img, geo, scale, y_center):
        """联系人列表行在头像列 (rx 0.105~0.155) 是否有头像 (彩色/亮像素)。
        分区头/索引字母行那里是纯色背景。"""
        import numpy as np
        try:
            arr = np.asarray(img.convert('RGB'))
        except Exception:
            return True
        W = geo['w']
        x0 = int(0.105 * W * scale)
        x1 = int(0.155 * W * scale)
        y0 = int((y_center - geo['y'] - 14) * scale)
        y1 = int((y_center - geo['y'] + 14) * scale)
        reg = arr[max(0, y0):y1, max(0, x0):x1]
        if reg.size == 0:
            return False
        mx = reg.max(axis=2).astype(int)
        mn = reg.min(axis=2).astype(int)
        colorful = int(((mx - mn) > 40).sum())
        bright = int((reg.mean(axis=2) > 90).sum())
        n = reg.shape[0] * reg.shape[1]
        # 实测标定: 好友行 (含灰色默认头像) bright 占比 ≥25%, 分区头 ≤19%,
        # 纯乱码行更低; 彩色照片头像另外用 colorful 占比兜底
        return bool(bright > n * 0.22 or colorful > n * 0.12)

    @_serialized
    def GetFriendDetails(self, n=10, timeout=0, save_head_image=False,
                         save_head_wait=0):
        """联系人列表 (OCR): 切到联系人页读左列名称。

        受 OCR 限制, 返回的是「当前可见的联系人名」近似列表 (表情/生僻字可能污染)。
        """
        self._ensure_main_window()
        self.SwitchToContact()
        time.sleep(0.8)
        img, rows, geo, scale = self._window_rows(activate=False, refresh=True)
        W, H = geo['w'], geo['h']
        x0 = geo['x'] + W * 0.055
        x1 = geo['x'] + W * 0.45
        items = [r for r in rows
                 if x0 <= r['lx'] <= x1 and r['ly'] > geo['y'] + H * 0.08]
        lines = self._cluster_lines(items)
        out = []
        for line in lines:
            self._tick()
            # 名字只取头像右侧的 token (头像本身会被 OCR 成乱字黏在行首)
            nm_toks = [t for t in line if t['lx'] >= geo['x'] + W * 0.155]
            nm = _clean_name(self._join([t['text'] for t in nm_toks]))
            if not nm:
                continue
            # 分区头: 行内含 > 箭头 token, 或名称含分区头字样 (头像乱字可能黏连)
            if any(t['text'].strip() in ('>', '》', '>》', '›') for t in line):
                continue
            if any(h in nm for h in self._CONTACT_HEADERS):
                continue
            if re.match(r'^[A-Za-z]$', nm):     # 分组索引字母
                continue
            # 真正的判据: 好友行有头像, 分区头/索引行没有
            if not self._has_avatar(img, geo, scale, line[0]['ly']):
                continue
            if nm in out:
                continue
            out.append(nm)
            if len(out) >= max(n, 1):
                break
        return [{'昵称': nm} for nm in out]

    # ---- 未实现的接口 (保持兼容, 明确报错) ---------------------------------
    def GetAllRecentGroups(self):
        return [s.info['name'] for s in self.GetSession()]

    def GetSubWindow(self, nickname=None):
        return None

    def GetAllSubWindow(self):
        return []

    def GetNewFriends(self, acceptable=None):
        return []

    def SendUrlCard(self, url=None, friends=None, message=None, timeout=3):
        return WxResponse(False, 'Linux 版暂不支持发送链接卡片')

    def AddListenChat(self, nickname=None, callback=None):
        return None

    def GetListenMessage(self):
        return []

    def GetNextNewMessage(self, filter_mute=False):
        # 轮询会话列表红点
        out = {}
        try:
            for s in self.GetSession():
                if s.info['isnew']:
                    out[s.info['name']] = []
        except Exception:
            pass
        return out

    def KeepRunning(self):
        import threading
        while True:
            time.sleep(3600)

    @_serialized
    def SwitchToChat(self):
        geo = self._cached_geo()
        wdt.click(geo['x'] + geo['w'] * NAV_X, geo['y'] + geo['h'] * NAV_CHAT_Y)
        time.sleep(0.4)
        self._snap = None
        return True

    @_serialized
    def SwitchToContact(self):
        geo = self._cached_geo()
        wdt.click(geo['x'] + geo['w'] * NAV_X, geo['y'] + geo['h'] * NAV_CONTACT_Y)
        time.sleep(0.5)
        self._snap = None
        return True

    # ---- 工具 ------------------------------------------------------------
    @staticmethod
    def _clipboard(text: str):
        import subprocess
        p = subprocess.Popen(['wl-copy', '--', text], env=winman.session_env(),
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        return p


class Message:
    def __init__(self, content='', sender='', type='text', attr='friend',
                 chat=None, time=''):
        self.content = content
        self.sender = sender
        self.type = type
        self.attr = attr
        self.chat = chat or {}
        self.time = time

    def __repr__(self):
        return f'<Message {self.sender}: {self.content[:30]!r}>'


class Chat:
    """子窗口占位 (Linux 版主窗口单实例)。"""

    def __init__(self, core=None, name=''):
        self.core = core
        self.name = name

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        return self.core.SendMsg(msg, who=self.name or who, clear=clear)

    def GetAllMessage(self):
        return self.core.GetAllMessage()

    def ChatInfo(self):
        return {'chat_name': self.name}
