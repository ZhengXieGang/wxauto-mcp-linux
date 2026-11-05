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
import os
import re
import sys
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
CACHE_TTL = 4.0
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
                '+', '口。', '”全'}

# ---- UI 骨架固定逻辑坐标 --------------------------------------------
# 实测 841×545 与 1707×1023 两种窗口、不同 DPI 下完全一致: 微信 Linux 的
# 左栏/面板/输入区都是固定逻辑尺寸, 不随窗口大小或缩放拉伸。
# 旧版按窗口比例定位 (0.21H/0.30H/0.42W...), 窗口一变 (尤其最大化) 全部漂移。
NAV_CHAT = (30, 115)       # 左栏导航: 聊天图标中心
NAV_CONTACT = (30, 163)    # 左栏导航: 联系人图标中心
PANEL_X1 = 300             # 会话/联系人列表面板右缘 (固定宽, 不随窗口拉伸)
SEARCH_FALLBACK = (100, 50, 27, 17)   # 搜索框文字兜底锚点 (lx, ly, lw, lh)
TIME_COL_W = 62            # 会话行右侧时间列宽
ICON_COL_W = 26            # 静音铃铛图标列宽
AVATAR_X0, AVATAR_X1 = 55, 100          # 会话行头像列 (红点检测)
CT_AVATAR_X0, CT_AVATAR_X1 = 68, 106    # 联系人页头像列
INPUT_H = 145              # 底部输入区固定高 (聊天区下界 = 窗口底 - INPUT_H)
DROPDOWN_X1 = 358          # 搜索下拉面板右缘
DROPDOWN_H = 470           # 搜索下拉最大可视高


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
            # 点输入框区域 (固定在底部, 距底 70 在输入区内, 任何窗口高度都对)
            wdt.click(geo['x'] + geo['w'] * 0.5, geo['y'] + geo['h'] - 70)
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

    @staticmethod
    def _dedup_rows(rows):
        """v6 偶尔对同一文本出两个偏移几个像素的检测框 —— 名称黏连
        ('张三张三')/时间双读的根源。同视觉行内水平重叠超过较小框 55%
        的两个框, 只留 conf 高的那个。"""
        out = []
        for r in sorted(rows, key=lambda r: -r.get('conf', 0)):
            dup = False
            for k in out:
                ov = min(r['lx'] + r['lw'], k['lx'] + k['lw']) - max(r['lx'], k['lx'])
                if (ov > min(r['lw'], k['lw']) * 0.55
                        and abs(r['ly'] - k['ly']) < max(r['lh'], k['lh'])):
                    dup = True
                    break
            if not dup:
                out.append(r)
        return out

    def _window_rows(self, activate=True, refresh=False, psm=3):
        """截图 + 全窗口 OCR (带缓存)。返回 (img, rows, geo, scale)。"""
        now = time.monotonic()
        if not refresh and self._snap and now - self._snap[0] < CACHE_TTL:
            _, img, rows, geo, scale = self._snap
            if rows is None:
                # _shot 只存图不存行; 这里 OCR 后必须转窗口逻辑坐标 + 去重
                # (旧版漏转换, 原始图像坐标行流进 _anchors 曾致 KeyError)
                rows = self._dedup_rows(
                    self._to_window_rows(ocr.ocr(img, psm=psm), geo, scale))
                self._snap = (self._snap[0], img, rows, geo, scale)
            return img, rows, geo, scale
        img, geo, scale = self._shot(activate=activate, refresh=True)
        rows = self._dedup_rows(self._to_window_rows(ocr.ocr(img, psm=psm), geo, scale))
        self._snap = (time.monotonic(), img, rows, geo, scale)
        return img, rows, geo, scale

    def _ocr_region(self, img, geo, scale, x0f, y0f, x1f, y1f, psm=3):
        """OCR 窗口内比例子区域, 返回窗口逻辑坐标行 (裁剪 OCR 比全窗口快数倍)。"""
        W, H = geo['w'], geo['h']
        box = (int(x0f * W * scale), int(y0f * H * scale),
               int(x1f * W * scale), int(y1f * H * scale))
        crop = img.crop(box)
        rows = ocr.ocr(crop)
        for r in rows:
            r['lx'] = geo['x'] + x0f * W + r['x'] / scale
            r['ly'] = geo['y'] + y0f * H + r['y'] / scale
            r['lw'] = r['w'] / scale
            r['lh'] = r['h'] / scale
        return self._dedup_rows(rows)

    def _state(self, activate=True, refresh=False):
        """读一次窗口状态: (img, rows, geo, scale, anchors)。

        搜索框的灰色占位字「搜索」对比度太低主 pass 可能漏检 → 只在
        锚点确实缺失时才补一遍顶部条带灰度拉伸 OCR (省 ~2s/次)。
        """
        img, rows, geo, scale = self._window_rows(activate=activate, refresh=refresh)
        a = self._anchors(rows, geo, img, scale)
        if 'search' not in a:
            W, H = geo['w'], geo['h']
            try:
                band = img.crop((int(0.04 * W * scale), int(0.02 * H * scale),
                                 int(0.42 * W * scale), int(0.16 * H * scale)))
                st = ocr.stretch_gray(band)
                ox, oy = 0.04 * W, 0.02 * H
                extra = ocr.ocr(st)
                for r in extra:
                    r['lx'] = geo['x'] + ox + r['x'] / scale
                    r['ly'] = geo['y'] + oy + r['y'] / scale
                    r['lw'] = r['w'] / scale
                    r['lh'] = r['h'] / scale
                rows = self._dedup_rows(rows + extra)
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
            if (r['text'].strip() == '搜索'
                    and r['ly'] < geo['y'] + 150 and r['lx'] < geo['x'] + 220):
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
            xgate = sx + a['search']['lw'] + 12
        toks = [r for r in rows
                if r['lx'] > xgate
                and abs(r['ly'] - sy) < 25
                and self._tok_ok(r)]
        # 检测框可能重复 (同一标题被框两次: '文件传'+'文件传输助手') →
        # 丢弃与已有框水平重叠超过一半的框
        kept = []
        for r in sorted(toks, key=lambda r: r['lw'], reverse=True):
            dup = False
            for k in kept:
                ov = min(r['lx'] + r['lw'], k['lx'] + k['lw']) - max(r['lx'], k['lx'])
                if ov > r['lw'] * 0.5:
                    dup = True
                    break
            if not dup:
                kept.append(r)
        toks = sorted(kept, key=lambda r: r['lx'])
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
            ry = (s['ly'] - geo['y']) / geo['h']
            try:
                strip = self._ocr_region(img, geo, scale,
                                         150 / geo['w'], max(0.0, ry - 6 / geo['h']),
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

    # ---- 状态恢复 -------------------------------------------------------
    def _recover_main_page(self):
        """回到主界面 (聊天列表): 聚焦搜索框 → 清空关键词 → 点聊天图标。

        覆盖三种残留状态: 搜索下拉张开 / 全窗网络结果页 (无导航栏) /
        左侧栏在联系人页。搜索框位置固定 (左栏顶部), 清空关键词后
        微信自动退出搜索态。绝不能用 Escape —— 会把窗口藏进托盘。
        """
        try:
            geo = self._cached_geo()
            # 搜索框是固定逻辑位置 (窗口左上角), 不随窗口尺寸变化
            wdt.click(geo['x'] + 150, geo['y'] + 58)
            time.sleep(0.4)
            wdt.key_ctrl_a()
            time.sleep(0.12)
            wdt.key_delete()
            time.sleep(0.5)
            wdt.click(geo['x'] + NAV_CHAT[0], geo['y'] + NAV_CHAT[1])
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
            x1 = rect[2] - 4
        elif 'title' in a:
            x1 = a['title']['lx'] - 8
        else:
            x1 = geo['x'] + PANEL_X1
        x0 = geo['x'] + 96     # 头像列 (55..95) 右缘: 头像上的 OCR 幻觉字不黏进名字
        y0 = sy + a['search']['lh'] * 1.8          # 搜索栏以下
        items = [r for r in rows if x0 <= r['lx'] <= x1 and r['ly'] >= y0]
        lines = self._cluster_lines(items)
        timex0 = x1 - TIME_COL_W                   # 时间列起始 x
        icontimex = x1 - ICON_COL_W                # 静音铃铛图标列
        avx0, avx1 = geo['x'] + AVATAR_X0, geo['x'] + AVATAR_X1  # 头像列 (红点检测)
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
            if head_time:
                # 时间列里所有 time 形 token 都剔除 (v6 会出重复框, 只剔
                # 首个会把第二个时间框黏进名字)
                rest = [t for t in texts
                        if not (t['lx'] >= timex0 and self._looks_like_time(t['text'].strip()))]
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
        # 只看左侧会话面板 (固定逻辑宽 ~315, 物理像素): 聊天区绿色气泡也命中
        # 绿色, 旧版按窗口宽 42% 裁剪 —— 最大化窗口时气泡会越界面板把右界推崩
        panel_w = min(int(315 * scale), Wpx)
        mask[:, panel_w:] = False
        # 左侧 52 逻辑px 是导航栏: 聊天图标选中时也是绿色, 且与第一条
        # 会话行 y 重叠 (首行中心 ~104, 图标 ~115) —— 不裁掉会把矩形
        # x0 拉到 22, 后续条带裁剪全歪
        mask[:, :int(52 * scale)] = False
        counts = mask.sum(axis=1)
        ys = np.where(counts > panel_w * 0.4)[0]
        if len(ys) == 0:
            return None
        y_med = float(np.median(ys))
        band = mask[max(0, int(y_med - 22 * scale)):int(y_med + 22 * scale)]
        cols = np.where(band.sum(axis=0) > 6 * scale)[0]
        if len(cols) == 0:
            return None
        rect = (geo['y'] + y_med / scale,
                geo['x'] + float(cols.min()) / scale,
                geo['x'] + float(cols.max()) / scale)
        if rect[2] - rect[1] < 150:    # 退化 (固定逻辑宽判据: 面板宽 ~240, 窗口再宽也不变)
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
        ys = np.where(mask.sum(axis=1) > 20 * scale)[0]
        segs = []
        for y in ys:
            if segs and y - segs[-1][1] <= 6:
                segs[-1][1] = y
            else:
                segs.append([y, y])
        out = []
        from PIL import Image as _I
        for y0, y1 in segs:
            if y1 - y0 < max(12, 10 * scale):    # 图标碎片
                continue
            band = mask[y0:y1 + 1]
            colok = band.sum(axis=0) > 2
            xs = np.where(colok)[0]
            if len(xs) == 0 or xs.max() - xs.min() < 16 * scale:
                continue
            x0, x1 = int(xs.min()), int(xs.max())
            # 原样裁剪 (带边距) 走常规 det+rec: v6 对长气泡直接可读
            crop = img.crop((max(0, x0 - 8), max(0, y0 - 8),
                             min(arr.shape[1], x1 + 8), min(Hpx, y1 + 8)))
            txt = ''
            try:
                parts = [t['text'] for t in ocr.ocr(crop) if t['text'].strip()]
                txt = ''.join(parts)
            except Exception:
                parts = []
            if not txt:
                # 短气泡 det 漏检 → 深色像素定文字 bbox, 原样紧裁 rec-only
                gray = np.asarray(img.convert('L'))
                sub = gray[y0:y1 + 1, x0:x1 + 1]
                dm = (sub < 115) & colok[x0:x1 + 1][None, :]
                if int(dm.sum()) < 15 * scale:
                    continue
                dys, dxs = np.where(dm)
                pad = 5
                cy0 = max(0, y0 + int(dys.min()) - pad)
                cy1 = min(Hpx, y0 + int(dys.max()) + pad)
                cx0 = max(0, x0 + int(dxs.min()) - pad)
                cx1 = x0 + int(dxs.max()) + pad
                try:
                    rec = ocr.ocr_rec_line(img.crop((cx0, cy0, cx1, cy1)))
                    txt = ''.join(t['text'] for t in rec if t['text'].strip())
                except Exception:
                    continue
            if not txt:
                continue
            out.append({'ly': geo['y'] + (y0 + y1) / 2 / scale,
                        'ly0': geo['y'] + y0 / scale,
                        'ly1': geo['y'] + y1 / scale,
                        'lx': geo['x'] + x0 / scale,
                        'lxr': geo['x'] + x1 / scale,
                        'text': txt})
        return out

    def _green_row_read(self, img, geo, scale, rect):
        """绿色选中行的名字条带 OCR (psm7 单行)。

        全窗口 psm3 对绿底白字经常整行丢失; 条带只裁名字行 (绿块上半部,
        跳过头像列、排除时间列与内容预览行), 实测 3/3 稳定读出。
        """
        try:
            x0 = int((rect[1] + 40 - geo['x']) * scale)
            x1 = int((rect[2] - TIME_COL_W - geo['x']) * scale)
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
    def _current_chat_fast(self, refresh=False):
        """快速当前聊天名: 只 OCR 顶部标题条带 (~1.2s, 全窗读 ~6s)。

        条带左界取 150 而不是面板右缘 300: 标题离面板右缘只有 ~13px,
        检测器对贴裁剪边文本会整块漏检 (实测)。
        标题是当前聊天的权威指示; 读到且匹配目标即可采信, 读不到或不匹配
        由调用方走慢路径权威复核 —— 快路径的误判方向只会「更严格」。
        """
        try:
            img, geo, scale = self._shot(activate=False, refresh=refresh)
            strip = self._ocr_region(img, geo, scale,
                                     150 / geo['w'], 25 / geo['h'],
                                     1.0, 92 / geo['h'], psm=6)
            # 只留标题行: y < 74 (聊天区首行日期分隔符在 ~77) 且无时间样文本
            strip = [r for r in strip
                     if r['ly'] < geo['y'] + 74
                     and r['text'].strip() != '搜索'
                     and not re.search(r'\d{1,2}[:：.]\d{2}', r['text'])]
            strip.sort(key=lambda r: r['lx'])
            name = _clean_name(self._join([t['text'] for t in strip
                                           if self._tok_ok(t)]))
            return name if len(name) >= 2 else ''
        except Exception:
            return ''

    def _current_chat(self, refresh=False, skip_fast=False):
        """当前选中会话名: 标题 (对比度高) → 绿色高亮行 → 标题条带。"""
        if refresh:
            self._snap = None           # 强制下一读拿新帧
        if not skip_fast:
            try:
                name = self._current_chat_fast()
            except Exception:
                name = ''
            if name:
                return name
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
    SEARCH_ACTION_WORDS = ('打开', '已读', '恢复', '数据', '小程序', '搜索',
                           '帮助与反馈', '解除', '自助', '辅助', '验证解封')

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
            # 网络搜索分区: 头行特征是「搜索/网络 + 结果」的组合, 清洗后仍能命中;
            # 子行带 🔍 前缀 (OCR 成 'Q'), 且都出现在头行之后 → 全部排除
            if '网络' in name and '结果' in name:
                skip_rest = True
                continue
            if skip_rest:
                continue
            if name in self.SEARCH_SECTIONS:
                # 分区头 (功能/联系人/群聊...) 后首个匹配行 = 结构定位 (trust 0)
                j = i + 1
                while j < len(lines) and (not names[j]
                                          or names[j] in self.SEARCH_SECTIONS
                                          or ('网络' in names[j] and '结果' in names[j])):
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
        """搜索框下方下拉区域的裁剪 OCR (比全窗口 OCR 快且准)。

        下拉面板是固定逻辑宽高; 旧版按窗口比例取到 0.9H, 最大化窗口时
        会把下拉以外聊天区的消息读进候选。
        """
        x0 = max(geo['x'] + 40, s['lx'] - 20)
        x1 = geo['x'] + DROPDOWN_X1
        y0 = s['ly'] + s['lh'] + 4
        y1 = min(geo['y'] + geo['h'] - INPUT_H, s['ly'] + DROPDOWN_H)
        return self._ocr_region(img, geo, scale,
                                max(0.0, (x0 - geo['x']) / geo['w']),
                                max(0.0, (y0 - geo['y']) / geo['h']),
                                min(1.0, (x1 - geo['x']) / geo['w']),
                                min(1.0, (y1 - geo['y']) / geo['h']))

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
                cur0 = self._current_chat_fast()
                if not cur0:
                    cur0 = self._current_chat(skip_fast=True)
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
                wdt.click(geo['x'] + 170, hit['_y'] + 5)   # 行中部 (面板固定宽)
                time.sleep(1.1)
                ok, cur = self._verify_switched(who)
                if ok:
                    self._last_chat = who
                    return WxResponse(True, f'切换到 {who} (会话列表)')
                self._tick()
        # 1. 搜索: 搜索框是固定逻辑位置 (窗口左上, 实测两种窗口尺寸一致),
        #    直接固定坐标点击, 免去一次全窗 OCR (~8s)
        geo = self._cached_geo()
        s = {'lx': geo['x'] + 94, 'ly': geo['y'] + 47, 'lw': 27, 'lh': 17,
             'text': '搜索', 'conf': 0}
        self._snap = None
        wdt.click(s['lx'] + s['lw'] / 2, s['ly'] + s['lh'] / 2)
        time.sleep(0.6)
        # 2. 清空并粘贴联系人名
        wdt.key_ctrl_a()
        time.sleep(0.15)
        self._clipboard(who)
        wdt.key_ctrl_v()
        time.sleep(0.8)
        self._tick()
        # 3. 裁剪 OCR 下拉 (psm3+psm6 双通道); 网络搜索结果可能把联系人
        #    顶下去 → 找不到就下滚列表再找一轮
        best = None
        prev_lines_sig = None
        for attempt in range(3):
            # 下拉可能尚未加载完 (网络搜索段是异步拉取的) → 首轮多等一会
            time.sleep(0.6 if attempt == 0 else 0.3)
            img, geo, scale = self._shot(activate=False, refresh=True)
            cand = self._dropdown_rows(img, geo, scale, s)
            c = self._search_candidate(cand, who, exact)
            if c is not None and (best is None
                                  or (c[0], -c[1]) < (best[0], -best[1])):
                best = c
            if best is not None:
                break
            self._tick()
            # 下拉内容与上一轮完全一致 → 不会再有新结果 (名字不存在),
            # 不浪费时间滚动 (注: 滚动只对“联系人被顶下去”的场景有意义,
            # 那种情况下每轮内容会不同)
            sig = tuple(sorted((round(l[0]['ly']), self._join([t['text'] for t in l]))
                               for l in self._cluster_lines(cand)))
            if sig == prev_lines_sig:
                break
            prev_lines_sig = sig
            if attempt >= 1:
                wdt.mousemove(geo['x'] + 180, geo['y'] + 300)
                wdt.run(['scroll', '--', '0', '400'])
                time.sleep(0.7)
        if best is None:
            self._recover_main_page()
            return WxResponse(False,
                              f'搜索结果中未找到: {who} (下拉 OCR 未匹配到该名称)')
        # 4. 点击后必须验证切换成功 —— 防止点击落空后消息发进别的会话 (安全关键)
        self._snap = None
        wdt.click(best[2][0], best[2][1])
        time.sleep(1.1)
        ok, cur = self._verify_switched(who)
        if not ok:
            self._recover_main_page()
            self._last_chat = None
            return WxResponse(False, f'切换验证失败: 当前为 {cur!r}, 已取消 (目标 {who!r})')
        self._last_chat = who
        return WxResponse(True, f'切换到 {who}')

    def _verify_switched(self, who):
        """切换点击后的验证: 快读 → 短等重读 (渲染可能未完成) → 刷新全读。

        返回 (ok, cur)。误判方向只会「拒真」(多等/多读), 不会「纳假」。
        """
        cur = self._current_chat_fast()
        if cur and self._name_match(who, cur):
            return True, cur
        time.sleep(0.7)
        cur = self._current_chat_fast(refresh=True)
        if cur and self._name_match(who, cur):
            return True, cur
        cur = self._current_chat(refresh=True, skip_fast=True)
        return bool(cur) and self._name_match(who, cur), cur

    def _input_point(self, activate=False):
        """定位输入框点击点: 优先「发送」按钮上方 (用已缓存的行), 退化用
        输入区固定几何 (底部 140 逻辑高, 距底 60 落在文字区中间)。"""
        geo = self._cached_geo()
        rows = self._snap[2] if (self._snap and self._snap[2] is not None) else None
        send = None
        for r in (rows or []):
            if r['text'].strip() == '发送' and r['ly'] > geo['y'] + geo['h'] * 0.7:
                send = r
                break
        if send is not None:
            return (send['lx'] - geo['w'] * 0.30, send['ly'] - max(send['lh'] * 2.5, 30)), geo
        return (geo['x'] + geo['w'] * 0.55, geo['y'] + geo['h'] - 60), geo

    def _verify_target(self, who: str) -> WxResponse | None:
        """发送前二次验证当前聊天确实是 who; 不匹配则拒绝 (防误发)。

        快路径标题条带 (~0s, 命中缓存) 匹配即通过; 不匹配再走全窗权威复核。
        """
        cur = self._current_chat_fast()
        if cur and self._name_match(who, cur):
            return None
        cur = self._current_chat()
        if cur and self._name_match(who, cur):
            return None
        return WxResponse(False, f'安全拦截: 当前聊天 {cur!r} ≠ {who!r}, 未发送')

    def _main_page_ok(self, img, geo, scale) -> bool:
        """轻量主界面判定: 顶部条带里能看到搜索框 (~1.7s, 全窗 OCR 的 1/5)。"""
        try:
            band = self._ocr_region(img, geo, scale, 0.0, 0.0,
                                    min(1.0, 320 / geo['w']), min(1.0, 95 / geo['h']),
                                    psm=6)
            return any('搜索' in r['text'] for r in band)
        except Exception:
            return False

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
            img, g3, scale3 = self._shot(activate=True, refresh=True)
        except Exception as e:
            return WxResponse(False, f'发送中止: 微信窗口不可用 ({e})')
        if not self._main_page_ok(img, g3, scale3):
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

    _DLG_GEO_JS = (
        'const w = (() => { for (const x of workspace.windowList()) '
        'if (x.caption.indexOf("选择文件") >= 0) return x; })();'
        'if (w) { const g = w.frameGeometry; '
        'console.info("WXDLG|" + Math.round(g.x) + "|" + Math.round(g.y) + "|" '
        '+ Math.round(g.width) + "|" + Math.round(g.height)); '
        '} else { console.info("WXDLG|none"); }'
    )

    def _file_dialog_geo(self):
        """「选择文件」对话框的屏幕逻辑几何; 无对话框返回 None。"""
        out = winman.kwin_run_js(self._DLG_GEO_JS, 'WXDLG', wait=0.4)
        if not out or out == 'none':
            return None
        p = out.split('|')
        if len(p) < 4:
            return None
        return {'x': float(p[0]), 'y': float(p[1]),
                'w': float(p[2]), 'h': float(p[3])}

    # 「发送文件」按钮: 输入工具栏第 3 个图标, 固定逻辑坐标
    # (面板右缘 300 起的表情/收藏之后), 距窗口底 ~31
    FILE_BTN_OFF = (300 + 104, -31)
    # 对话框底部「名称(N)」输入框, 相对对话框几何的固定位置
    DLG_NAME_FIELD = (0.62, 0.82)

    @_serialized
    def SendFiles(self, filepath, who=None, exact=False, max_retries=3) -> WxResponse:
        """发送文件: 「发送文件」按钮 → 系统文件对话框 → 名称栏粘贴路径。

        微信 Linux 输入框不支持文件粘贴 (uri-list 只会被当纯文本),
        唯一可靠通道是文件对话框。附件挂上后 OCR 校验输入区确实出现
        文件卡片, 没有就不按回车 (防止误发草稿)。
        """
        if who:
            r = self.ChatWith(who, exact=exact)
            if not r.success:
                return r
            guard = self._verify_target(who)
            if guard is not None:
                return guard
        paths = [str(p) for p in (filepath if isinstance(filepath, list) else [filepath])]
        for p in paths:
            if not Path(p).exists():
                return WxResponse(False, f'文件不存在: {p}')
        self._tick()
        winman.activate_wechat()
        self._snap = None
        try:
            img, g3, scale3 = self._shot(activate=True, refresh=True)
        except Exception as e:
            return WxResponse(False, f'发送中止: 微信窗口不可用 ({e})')
        if not self._main_page_ok(img, g3, scale3):
            return WxResponse(False, '发送中止: 微信窗口不在主界面 (可能未登录或被遮挡)')
        geo = self._cached_geo()
        # 输入框清空 (clear=True 语义; 也防止回车误发残留草稿)
        wdt.click(geo['x'] + geo['w'] * 0.55, geo['y'] + geo['h'] - 60)
        time.sleep(0.3)
        wdt.key_ctrl_a()
        time.sleep(0.15)
        wdt.key_delete()
        time.sleep(0.3)
        attached = 0
        for path in paths:
            self._tick()
            # 1. 点「发送文件」按钮
            wdt.click(geo['x'] + self.FILE_BTN_OFF[0], geo['y'] + geo['h'] + self.FILE_BTN_OFF[1])
            time.sleep(1.6)
            # 2. KWin 定位对话框
            dlg = None
            for _ in range(3):
                dlg = self._file_dialog_geo()
                if dlg is not None:
                    break
                time.sleep(0.8)
            if dlg is None:
                break
            # 3. 名称(N) 栏粘贴绝对路径 (打字会触发补全弹层劫持回车)
            wdt.click(dlg['x'] + dlg['w'] * self.DLG_NAME_FIELD[0],
                      dlg['y'] + dlg['h'] * self.DLG_NAME_FIELD[1])
            time.sleep(0.5)
            wdt.key_ctrl_a()
            time.sleep(0.15)
            self._clipboard(path)
            wdt.key_ctrl_v()
            time.sleep(0.6)
            wdt.key_enter()
            time.sleep(1.4)
            attached += 1
        if attached == 0:
            return WxResponse(False, '发送中止: 文件对话框未打开或未挂上附件')
        # 4. 校验输入区确实出现了附件卡片 (OCR 找文件名), 没有就不发送
        img2, g4, sc4 = self._shot(activate=False, refresh=True)
        inp_rows = self._ocr_region(img2, g4, sc4, 0.28,
                                    max(0.0, (g4['h'] - 145) / g4['h']), 0.9, 0.98)
        inp_text = ''.join(r['text'] for r in inp_rows)
        basenames = [Path(p).name for p in paths[:attached]]
        hit = [b for b in basenames
               if any(ch in inp_text for ch in {b[:8], b[-8:]} if ch)]
        if not hit:
            return WxResponse(False,
                              '发送中止: 附件未出现在输入区 (对话框流程失败), '
                              '未回车发送; 输入区已保留现场可人工检查')
        # 5. 回车发送
        wdt.click(geo['x'] + geo['w'] * 0.55, geo['y'] + geo['h'] - 60)
        time.sleep(0.3)
        wdt.key_enter()
        time.sleep(1.0)
        self._snap = None
        return WxResponse(True, f'发送 {attached} 个文件')

    @_serialized
    def SendImg(self, filepath, who=None, exact=False) -> WxResponse:
        """发送图片: 多 MIME 剪贴板 (image/png 等) → 输入框粘贴。

        图片是输入框粘贴处理器唯一支持的附件类型 (文件不行, 见 SendFiles);
        粘贴后校验输入区确实出现内容再回车。
        """
        if who:
            r = self.ChatWith(who, exact=exact)
            if not r.success:
                return r
            guard = self._verify_target(who)
            if guard is not None:
                return guard
        path = str(filepath if not isinstance(filepath, list) else filepath[0])
        if not Path(path).exists():
            return WxResponse(False, f'图片不存在: {path}')
        self._tick()
        winman.activate_wechat()
        self._snap = None
        try:
            img, g3, scale3 = self._shot(activate=True, refresh=True)
        except Exception as e:
            return WxResponse(False, f'发送中止: 微信窗口不可用 ({e})')
        if not self._main_page_ok(img, g3, scale3):
            return WxResponse(False, '发送中止: 微信窗口不在主界面 (可能未登录或被遮挡)')
        geo = self._cached_geo()
        wdt.click(geo['x'] + geo['w'] * 0.55, geo['y'] + geo['h'] - 60)
        time.sleep(0.3)
        wdt.key_ctrl_a()
        time.sleep(0.15)
        wdt.key_delete()
        time.sleep(0.3)
        # 空输入框基线 → 挂剪贴板 → 粘贴 → 像素差校验
        base_img, _, _ = self._shot(activate=False, refresh=True)
        clip_proc = self._clip_file(path)
        try:
            wdt.key_ctrl_v()
            time.sleep(1.6)
            img2, g4, sc4 = self._shot(activate=False, refresh=True)
            if not self._input_nonempty(img2, g4, sc4):
                return WxResponse(False, '发送中止: 图片未进入输入框 (粘贴无效果), 未回车')
        finally:
            clip_proc.terminate()
        wdt.click(geo['x'] + geo['w'] * 0.55, geo['y'] + geo['h'] - 60)
        time.sleep(0.3)
        wdt.key_enter()
        time.sleep(1.0)
        self._snap = None
        return WxResponse(True, '发送图片成功')

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
        rect = self._selected_session_rect(img, geo, scale)
        if not t and rect is None:
            # 没有标题也没有选中会话 = 没有打开的聊天 (恢复后仍如此)。
            # 此时聊天区若 OCR 出文字, 多半是悬浮其上的系统通知 toast,
            # 读出来只会污染消息列表 → 直接返回空。
            return []
        chat_name = self._last_chat or ''
        if t:
            near = [r for r in rows
                    if abs(r['ly'] - t['ly']) < 20
                    and t['lx'] <= r['lx'] <= t['lx'] + t['lw'] + 60
                    and self._tok_ok(r)]
            near.sort(key=lambda r: r['lx'])
            chat_name = _clean_name(self._join([r['text'] for r in near])) or chat_name
        if not chat_name:
            chat_name = self._title_name(a, img, geo, scale) or self._last_chat or ''
        # 聊天区左缘: 标题锚点 > 面板固定右缘 (面板宽不随窗口变, 旧版
        # 0.27W 在最大化窗口会把聊天区左半截排除掉)
        x0 = (t['lx'] - 15) if t else geo['x'] + PANEL_X1 + 6
        # 若能定位到会话面板右缘 (绿色高亮行), 用它排除会话列泄漏
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None and rect[2] < (t['lx'] if t else x0 + W):
            x0 = rect[2] + W * 0.004
        y0 = (t['ly'] + t['lh'] * 2.2) if t else a['search']['ly'] + a['search']['lh'] * 3
        # 聊天区底界: 输入区是固定逻辑高 (实测 140), 旧版 max(120, 0.115H)
        # 在高窗口会把输入框草稿区包进来
        y1 = geo['y'] + H - INPUT_H
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
            # 只过滤贴上边的残条 (向上翻时视口顶部只露出气泡下半截);
            # 贴下边的完整气泡是真实新消息 (若被切半, 里面文字通常已残缺
            # 到匹配不上任何可信内容, 保留无害)
            if gb['ly0'] <= y0 + 4:
                continue
            merged.append({'ly': gb['ly'], 'lh': 20, 'lx': gb['lx'],
                           'text': gb['text'], 'green': True})
        merged.sort(key=lambda m: m['ly'])
        messages = []
        cur_sender = None
        last_line_y = None
        for m in merged:
            joined = m['text']
            first_ly = m['ly']
            if not m['green'] and first_ly - y0 < 12:
                continue        # 顶边切半的残行 (视口外只露出下半截)
            if m['green']:
                side = 'self'
            else:
                side = 'self' if m['lx'] > center + 8 else 'friend'
                # 时间行 (居中小字) / “N条新消息”浮标 → 忽略
                # 时间/日期分隔行 (居中小字): 纯时刻或带昨天/星期/日期前缀 → 忽略
                if re.match(r'^\d{1,2}:\d{2}$', joined) and \
                        m['lx'] + 20 > center - 120 and m['lx'] < center + 120:
                    continue
                if side == 'friend' and len(joined) < 22 and \
                        re.match(r'^(昨天|前天|今天|星期[\u4e00-\u9fff]|\d{1,2}月\d{1,2}日)',
                                 joined) and \
                        m['lx'] + 20 > center - 160 and m['lx'] < center + 160:
                    continue
                if '条新消息' in joined:
                    continue
                if '跳转到最新' in joined:
                    # 滚动后的回到底部浮标: 顺手点它, 让下次读取回到最新视图
                    try:
                        wdt.click(m['lx'] + 60, m['ly'])
                        time.sleep(0.5)
                        self._snap = None
                    except Exception:
                        pass
                    continue
            # 行间距很大 → 新气泡, 群聊里可能是新发言人的昵称行
            new_bubble = (last_line_y is None
                          or (first_ly - last_line_y) > m['lh'] * 2.2)
            if new_bubble and side == 'friend':
                cur_sender = None
            is_sender_name = (side == 'friend' and m['lh'] < 16
                              and len(joined) < 24
                              and m['lx'] < x0 + 200)   # 群昵称在左缘固定窄带内
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
        mx = geo['x'] + (PANEL_X1 + geo['w']) / 2   # 聊天区中线 (面板固定宽)
        my = geo['y'] + geo['h'] * 0.35
        wdt.mousemove(mx, my)
        time.sleep(0.2)
        for _ in range(5):
            self._tick()
            # 负值才是向上 (翻旧消息); 正值向下, 之前写反导致永远读同一段
            wdt.run(['scroll', '--', '0', '-600'])
            time.sleep(0.25)
        time.sleep(0.6)
        return self.GetAllMessage()

    # ---- 联系人 ---------------------------------------------------------
    # 联系人页分区头 (不算好友)
    _CONTACT_HEADERS = {'新的朋友', '仅聊天的朋友', '群聊', '标签', '公众号',
                        '公众号、服务号', '企业微信联系人', '通讯录管理',
                        '联系人', '朋友', '网络联系人'}

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
        # 联系人列表面板固定宽: 名字列 ~108..430。旧版按窗口比例
        # (0.055W..0.45W / 0.155W 门限), 最大化窗口时会把右侧详情面板
        # 文字读进来, 名字也会被 x 门限拦腰截断
        x0 = geo['x'] + 60
        x1 = geo['x'] + 430
        items = [r for r in rows
                 if x0 <= r['lx'] <= x1 and r['ly'] > geo['y'] + 90]
        lines = self._cluster_lines(items)
        out = []
        for line in lines:
            self._tick()
            # 分区头判定用整行文本 (头行的人数字 token 在名字门限右侧,
            # 只看 nm 会漏掉 → 人数 '35' 之类会漏进来)
            line_txt = self._join([t['text'] for t in line])
            # 名字只取头像右侧的名字列 token: 列起点 ~129 (固定布局),
            # 分区头文字在 ~95, 门限 108 挡不住 OCR 右移的乱字框 (曾漏出
            # '人152' 之类) → 收紧到 120
            nm_toks = [t for t in line if t['lx'] >= geo['x'] + 120]
            nm = _clean_name(self._join([t['text'] for t in nm_toks]))
            if not nm:
                continue
            # 分区头: 行内含 > 箭头 token, 或名称含分区头字样 (头像乱字可能黏连)
            if any(t['text'].strip() in ('>', '》', '>》', '›') for t in line):
                continue
            if any(h in line_txt for h in self._CONTACT_HEADERS):
                continue
            if re.match(r'^[A-Za-z]$', nm):     # 分组索引字母
                continue
            if re.match(r'^\d+$', nm):          # 分区头行右缘的人数
                continue
            # 注: 不再用「头像列像素」过滤 —— 分区头自带小图标、深色头像
            # 与背景难分, 最大化/换主题下判据全崩 (曾导致整个功能返回空)。
            # 文字过滤器已覆盖实测所有分区头/索引行。
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
        # 先激活: 后台窗口时 libei 点击会落到前台别的应用上 (实测踩坑)
        winman.activate_wechat()
        time.sleep(0.3)
        geo = self._cached_geo()
        wdt.click(geo['x'] + NAV_CHAT[0], geo['y'] + NAV_CHAT[1])
        time.sleep(0.4)
        self._snap = None
        return True

    @_serialized
    def SwitchToContact(self):
        winman.activate_wechat()
        time.sleep(0.3)
        geo = self._cached_geo()
        wdt.click(geo['x'] + NAV_CONTACT[0], geo['y'] + NAV_CONTACT[1])
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

    def _clip_file(self, path: str, live=12.0):
        """把图片挂上多 MIME 剪贴板 (子进程存活 live 秒供微信读取)。

        用 sys.executable 同一解释器 (pywayland 装在 venv 里)。
        阻塞直到子进程报 READY (selection 已挂上) 或超时报错 —— 否则
        ctrl+v 粘到的是上一个剪贴板残留, 会把垃圾当图片发出去。
        """
        import subprocess
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = winman.session_env()
        env['PYTHONPATH'] = src_dir + os.pathsep + env.get('PYTHONPATH', '')
        env['CLIP_LIVE'] = str(live)
        mod = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clip.py')
        p = subprocess.Popen([sys.executable, mod, path], env=env,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
        deadline = time.time() + 6
        ready = False
        while time.time() < deadline:
            line = p.stdout.readline().strip()
            if line == 'READY':
                ready = True
                break
            if p.poll() is not None:
                break
        if not ready:
            p.terminate()
            raise RuntimeError('剪贴板子进程未就绪 (pywayland/ext-data-control 不可用?)')
        return p

    @staticmethod
    def _input_nonempty(img, geo, scale):
        """输入区是否有内容 (图片卡片/文件卡片都会让像素明显偏离空背景)。"""
        import numpy as np
        try:
            arr = np.asarray(img.convert('RGB'))
        except Exception:
            return False
        Hpx, Wpx = arr.shape[:2]
        # 只看输入框文字区 (贴底 135..55): 再往下是常驻工具栏图标, 必然非空
        y0 = max(0, Hpx - int(135 * scale))
        y1 = max(0, Hpx - int(55 * scale))
        reg = arr[y0:y1, int(Wpx * 0.28):int(Wpx * 0.95)]
        if reg.size == 0:
            return False
        med = np.median(reg.reshape(-1, 3), axis=0)
        diff = np.abs(reg.astype(int) - med).sum(axis=2)
        return bool((diff > 60).sum() > 500)


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
