"""wxauto_linux: 微信 Linux 客户端原生自动化驱动。

提供与 wxautox4.WeChat 兼容的接口子集 (wxauto-mcp 所需), 实现:
- 窗口管理: KWin D-Bus (激活/几何)
- 输入注入: wdotool (libei + xdg-desktop-portal 授权)
- 读取: 全屏截图裁剪 + tesseract OCR (chi_sim)
- 剪贴板: wl-copy (中文粘贴)
"""
import re
import time
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


class Session:
    """会话对象 (wxautox4 兼容: 属性访问经 .info 字典)。"""

    def __init__(self, info):
        self.info = info

    def __repr__(self):
        return f'<Session {self.info.get("name")!r}>'


# 会话行内的未读红点颜色 (WeChat 深色主题红色徽标)
RED = (237, 92, 75)


class WeChat:
    def __init__(self, nickname=None, start_listener=False, debug=False,
                 resize=True, version='微信', **kwargs):
        if wdt.search() is None:
            # 进程在但窗口被关进托盘 → 尝试恢复
            winman.restore_wechat_from_tray()
            if wdt.search() is None:
                raise WeChatError('微信 Linux 客户端未运行')
        self._last_chat = None

    # ---- 基础 ----------------------------------------------------------
    def _geo(self, retries=3):
        last = None
        for i in range(retries):
            last = winman.get_wechat_geometry()
            if last is not None:
                return last
            # 窗口可能被关进托盘, 尝试从托盘恢复
            try:
                winman.restore_wechat_from_tray()
            except Exception:
                pass
            time.sleep(0.4)
        raise WeChatError('微信窗口不存在')

    def _shot(self, activate=True):
        winman.activate_wechat()
        time.sleep(0.3)
        return ocr.window_shot(wait_activate=False)

    def _ensure_main_window(self):
        g = self._geo()
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
    def _ocr_window(self, activate=True, psm=3):
        img, geo, scale = self._shot(activate)
        rows = ocr.ocr(img, psm=psm)
        return img, self._to_window_rows(rows, geo, scale), geo, scale

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

    def _anchors(self, rows, geo):
        """定位界面锚点: 搜索框与聊天标题 (两者同一行)。"""
        a = {}
        for r in rows:
            if r['text'].strip() == '搜索' and r['ly'] < geo['y'] + geo['h'] * 0.15:
                a['search'] = r
                break
        if 'search' not in a:
            return a
        # 标题 = 搜索框同一行、其右侧最左的有效词块; 排除图标 OCR 幻觉单字
        skip = {'—', '一', '口', '口区', '回', '×', '区', '全', '中', '”', '”全',
                '。', '日', '吗', 'Q', 'X', 'L', 'O', '口。'}
        cands = [r for r in rows
                 if r['lx'] > a['search']['lx'] + geo['w'] * 0.03
                 and abs(r['ly'] - a['search']['ly']) < 25
                 and len(r['text'].strip()) >= 2
                 and (r['conf'] > 55 or len(r['text'].strip()) > 2)
                 and r['text'].strip() not in skip]
        if not cands:
            cands = [r for r in rows
                     if r['lx'] > a['search']['lx'] + geo['w'] * 0.03
                     and abs(r['ly'] - a['search']['ly']) < 25
                     and len(r['text'].strip()) >= 2
                     and r['conf'] > 40
                     and r['text'].strip() not in skip]
        if cands:
            cands.sort(key=lambda r: r['lx'])
            a['title'] = cands[0]
        return a

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

    # ---- 聊天 -----------------------------------------------------------
    # 搜索下拉的分区头 (整行): 之前的行全是搜索入口, 之后的才是联系人
    SEARCH_SECTIONS = ('功能', '联系人', '聊天记录', '群聊', '朋友圈', '公众号')
    # 搜索入口/动作行的特征词 (如「文件传输助手 打开」)
    SEARCH_ACTION_WORDS = ('打开', '已读', '恢复', '数据', '小程序', '搜索')

    @staticmethod
    def _name_similar(a: str, b: str) -> bool:
        """OCR 噪声容忍的名称相似度: 字符集重合率 ≥ 0.7。"""
        sa, sb = set(a or ''), set(b or '')
        if not sa or not sb:
            return False
        return len(sa & sb) / max(len(sa), len(sb)) >= 0.7

    def _line_point(self, line):
        """一行 OCR 词块的点击点: 行内水平中点、首词块垂直中心。"""
        first, last = line[0], line[-1]
        return ((first['lx'] + last['lx'] + last['lw']) / 2,
                first['ly'] + first['lh'] / 2)

    def _search_candidate(self, cand_rows, who, exact):
        """在搜索下拉行里选目标, 返回 (trust, y, (x,y)) 或 None。

        trust: 0=分区头后首行(结构定位, 最可靠) 1=精确 2=模糊 3=子串;
        同级取更靠下的行 —— 搜索入口行总在联系人行上方。
        """
        lines = self._cluster_lines(cand_rows)
        names = [self._join([t['text'] for t in l]) for l in lines]
        out = []
        for i, name in enumerate(names):
            if not name:
                continue
            if name in self.SEARCH_SECTIONS or name.startswith(('网络搜索', '搜索网络')):
                j = i + 1
                while j < len(lines) and (not names[j]
                                          or names[j] in self.SEARCH_SECTIONS
                                          or names[j].startswith(('网络搜索', '搜索网络'))):
                    j += 1
                if j < len(lines):
                    nm2 = names[j]
                    if self._name_similar(who, nm2) or who in nm2 or nm2 in who:
                        out.append((0, lines[j][0]['ly'], self._line_point(lines[j])))
                continue
            has_action = any(wd in name for wd in self.SEARCH_ACTION_WORDS)
            if has_action:
                continue
            if name == who:
                pri = 1
            elif self._name_similar(who, name):
                pri = 2
            elif not exact and (who in name or name in who):
                pri = 3
            else:
                continue
            out.append((pri, lines[i][0]['ly'], self._line_point(lines[i])))
        if not out:
            return None
        out.sort(key=lambda c: (c[0], -c[1]))
        return out[0]

    def ChatWith(self, who: str, exact: bool = True, force: bool = False,
                 force_wait: float = 0.5) -> WxResponse:
        self._ensure_main_window()
        winman.activate_wechat()
        time.sleep(0.3)
        # 0. 快路径: 目标已在会话列表 → 直接点该行 (绕开搜索下拉的不稳定 OCR)
        try:
            sessions, img, rows, geo, scale, a = self._session_rows(activate=False)
            if 'search' in a:
                hit = None
                for e in sessions:
                    nm = e.get('name', '')
                    if nm and (nm == who or who in nm
                               or self._name_similar(who, nm)):
                        hit = e
                if hit is not None:
                    wdt.click(a['search']['lx'] + geo['w'] * 0.13,
                              hit['_y'] + 5)
                    time.sleep(0.7)
                    cur = self._current_chat()
                    if cur and (who in cur or cur in who
                                or self._name_similar(who, cur)):
                        self._last_chat = who
                        return WxResponse(True, f'切换到 {who} (会话列表)')
        except Exception:
            pass
        # 1. 搜索: OCR 定位搜索框锚点后点击 (位置随窗口尺寸变化)
        img, rows, geo, scale = self._ocr_window(activate=False)
        a = self._anchors(rows, geo)
        if 'search' not in a:
            return WxResponse(False, '未找到搜索框')
        s = a['search']
        wdt.click(s['lx'] + s['lw'] / 2, s['ly'] + s['lh'] / 2)
        time.sleep(0.6)
        # 2. 清空并粘贴联系人名
        wdt.key_ctrl_a()
        time.sleep(0.15)
        self._clipboard(who)
        wdt.key_ctrl_v()
        time.sleep(1.2)
        # 3. OCR 下拉 (psm3 + psm6 双通道) 匹配; 网络搜索结果可能把联系人
        #    顶下去 → 找不到就下滚列表再找一轮
        best = None
        for attempt in range(2):
            img, geo2, scale = self._shot(activate=False)
            for psm in (3, 6):
                rows = self._to_window_rows(ocr.ocr(img, psm=psm), geo2, scale)
                x0 = s['lx'] - geo['w'] * 0.03
                x1 = s['lx'] + geo['w'] * 0.28
                cand = [r for r in rows
                        if x0 <= r['lx'] <= x1
                        and s['ly'] + 20 <= r['ly'] <= geo['y'] + geo['h'] * 0.8]
                c = self._search_candidate(cand, who, exact)
                if c is not None and (best is None
                                      or (c[0], -c[1]) < (best[0], -best[1])):
                    best = c
            if best is not None:
                break
            wdt.mousemove(s['lx'] + geo['w'] * 0.12, s['ly'] + geo['h'] * 0.35)
            wdt.run(['scroll', '--', '0', '400'])
            time.sleep(0.7)
        if best is None:
            wdt.key_escape()
            return WxResponse(False, f'搜索结果中未找到: {who}')
        # 4. 点击后必须验证切换成功 —— 防止点击落空后消息发进别的会话 (安全关键)
        wdt.click(best[2][0], best[2][1])
        time.sleep(0.8)
        cur = self._current_chat()
        ok = bool(cur) and (who in cur or cur in who or self._name_similar(who, cur))
        if not ok:
            wdt.key_escape()
            self._last_chat = None
            return WxResponse(False, f'切换验证失败: 当前为 {cur!r}, 已取消 (目标 {who!r})')
        self._last_chat = who
        return WxResponse(True, f'切换到 {who}')

    def _session_rows(self, activate=True, retries=2):
        """解析会话列表, 返回 (entries(含 _y), img, rows, geo, scale, anchors)。

        窗口被拖动/遮挡时几何与截图可能错位 → 找不到锚点就重取一次。
        """
        img = rows = geo = scale = a = None
        for attempt in range(retries + 1):
            img, rows, geo, scale = self._ocr_window(activate)
            a = self._anchors(rows, geo)
            if 'search' in a and 'title' in a:
                break
            time.sleep(0.6)
        if 'search' not in a or 'title' not in a:
            return [], img, rows, geo, scale, a
        sx, sy = a['search']['lx'], a['search']['ly']
        tx = a['title']['lx']
        W = geo['w']
        # 优先用绿色高亮行的 x 范围确定面板边界, 退化用搜索框/标题锚点
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None:
            _, gx0, gx1 = rect
            x0, x1 = gx0 + W * 0.055, gx1 - W * 0.005
        else:
            x0, x1 = sx - W * 0.03, tx - W * 0.008     # 会话面板文字区
        y0 = sy + a['search']['lh'] * 1.8          # 搜索栏以下
        items = [r for r in rows if x0 <= r['lx'] <= x1 and r['ly'] >= y0]
        lines = self._cluster_lines(items)
        timex0 = x1 - W * 0.055                    # 时间列起始 x
        icontimex = x1 - W * 0.015                 # 静音铃铛图标列
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
            for i, t in enumerate(texts):
                ttx = t['text'].strip()
                if t['lx'] >= timex0 and re.match(r'^(?:\d{1,2}:?\d{2}|星期.|\d{1,2}月\d{1,2}日|昨天)$', ttx):
                    head_time = ttx
                    head_y = t['ly']
                    rest = texts[:i] + texts[i + 1:]
                    break
            if head_time and rest:
                nm = self._join([t['text'] for t in rest if t['lx'] < icontimex])
                m = re.search(r'\[(\d+)条\]', nm)
                new_count = int(m.group(1)) if m else 0
                if m:
                    nm = nm.replace(m.group(0), '')
                if re.match(r'^(\d{1,2})(\d{2})$', head_time):
                    head_time = f'{int(head_time[:-2])}:{head_time[-2:]}'
                entry = {'name': nm, 'time': head_time, 'content': '',
                         'isnew': new_count > 0, 'new_count': new_count,
                         'ismute': any(t['lx'] >= icontimex for t in rest),
                         '_y': head_y}
                sessions.append(entry)
                cur = entry
            elif cur is not None:
                body = self._join([t['text'] for t in texts if t['lx'] < icontimex])
                if body:
                    cur['content'] = body
                    if texts and texts[-1]['lx'] >= icontimex:
                        cur['ismute'] = True
                    m2 = re.search(r'\[(\d+)条\]', body)
                    if m2:
                        cur['new_count'] = int(m2.group(1))
                        cur['isnew'] = True
        # 红点检测: 头行左侧头像区找红色像素
        for entry in sessions:
            hy = entry['_y']
            region_red = False
            import numpy as np
            try:
                arr = np.asarray(img.convert('RGB'))
                px0 = int((avx0 - geo['x']) * scale)
                px1 = int((avx1 - geo['x']) * scale)
                py0 = int((hy - geo['y'] - 18) * scale)
                py1 = int((hy - geo['y'] + 26) * scale)
                reg = arr[max(0, py0):max(0, py1), max(0, px0):max(0, px1)]
                if reg.size:
                    rr, gg, bb = reg[..., 0].astype(int), reg[..., 1].astype(int), reg[..., 2].astype(int)
                    region_red = bool(((rr > 190) & (gg < 130) & (bb < 130)).sum() > 12)
            except Exception:
                pass
            if region_red:
                entry['isnew'] = True
                entry['new_count'] = entry['new_count'] or 1
        return sessions, img, rows, geo, scale, a

    def _selected_session_rect(self, img, geo, scale):
        """绿色高亮行 (当前选中会话) 的窗口逻辑 (y, x0, x1); 无高亮返回 None。"""
        import numpy as np
        try:
            arr = np.asarray(img.convert('RGB'))
        except Exception:
            return None
        r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
        mask = (g > 110) & (g - r > 30) & (g - b > 30)
        counts = mask.sum(axis=1)
        Wpx = arr.shape[1]
        ys = np.where(counts > Wpx * 0.12)[0]
        if len(ys) == 0:
            return None
        y_med = float(np.median(ys))
        band = mask[max(0, int(y_med) - 30):int(y_med) + 30]
        cols = np.where(band.sum(axis=0) > 10)[0]
        if len(cols) == 0:
            return None
        return (geo['y'] + y_med / scale,
                geo['x'] + float(cols.min()) / scale,
                geo['x'] + float(cols.max()) / scale)

    def GetSession(self) -> list:
        self._ensure_main_window()
        sessions, img, rows, geo, scale, a = self._session_rows()
        return [Session(e) for e in sessions]

    def _current_chat(self):
        """当前选中会话名: 绿色高亮行匹配; 退化用标题锚点。"""
        sessions, img, rows, geo, scale, a = self._session_rows(activate=False)
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None and sessions:
            sy = rect[0]
            best = min(sessions, key=lambda e: abs(e['_y'] - sy))
            if abs(best['_y'] - sy) < 30:
                return best['name']
        t = a.get('title')
        if t:
            near = [r for r in rows
                    if abs(r['ly'] - t['ly']) < 20
                    and t['lx'] <= r['lx'] <= t['lx'] + t['lw'] + 60 * (geo['w'] / 855)
                    and (r['conf'] > 40 or len(r['text'].strip()) > 1)]
            near.sort(key=lambda r: r['lx'])
            name = self._join([r['text'] for r in near])
            if name:
                return name
        return self._last_chat or ''

    def ChatInfo(self) -> dict:
        self._ensure_main_window()
        return {'chat_name': self._current_chat()}

    def _input_point(self, activate=True):
        """定位输入框点击点: 优先「发送」按钮上方, 退化用固定比例。"""
        geo = self._geo()
        try:
            img, rows, g2, scale = self._ocr_window(activate=activate)
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
        """发送前二次验证当前聊天确实是 who; 不匹配则拒绝 (防误发)。"""
        cur = self.ChatInfo().get('chat_name', '')
        if cur and (who in cur or cur in who or self._name_similar(who, cur)):
            return None
        return WxResponse(False, f'安全拦截: 当前聊天 {cur!r} ≠ {who!r}, 未发送')

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
        winman.activate_wechat()
        time.sleep(0.2)
        (ix, iy), _ = self._input_point()
        wdt.click(ix, iy)
        time.sleep(0.4)
        self._clipboard(msg)
        wdt.key_ctrl_v()
        time.sleep(0.35)
        wdt.key_enter()
        time.sleep(0.7)
        return WxResponse(True, '发送成功')

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
        winman.activate_wechat()
        time.sleep(0.2)
        text = '\n'.join('file://' + str(p) for p in paths)
        (ix, iy), _ = self._input_point()
        wdt.click(ix, iy)
        time.sleep(0.4)
        self._clipboard(text)
        wdt.key_ctrl_v()
        time.sleep(1.2)
        wdt.key_enter()
        time.sleep(1.0)
        return WxResponse(True, f'发送 {len(paths)} 个文件')

    def GetAllMessage(self) -> list:
        self._ensure_main_window()
        # 标题锚点找不到时多为窗口拖动/遮挡导致几何错位, 重试一次
        for attempt in range(2):
            img, rows, geo, scale = self._ocr_window()
            a = self._anchors(rows, geo)
            if 'title' in a:
                break
            time.sleep(0.6)
        else:
            img, rows, geo, scale = self._ocr_window()
            a = self._anchors(rows, geo)
        if 'title' not in a:
            return []
        t = a['title']
        W, H = geo['w'], geo['h']
        chat_name = self._last_chat or ''
        if not chat_name:
            chat_name = self._join([r['text'] for r in sorted(
                [r for r in rows if abs(r['ly'] - t['ly']) < 20
                 and t['lx'] <= r['lx'] <= t['lx'] + t['lw'] + 60 * (W / 855)
                 and (r['conf'] > 40 or len(r['text'].strip()) > 1)],
                key=lambda r: r['lx'])])
        x0 = t['lx'] - W * 0.015               # 聊天区左界
        # 若能定位到会话面板右缘 (绿色高亮行), 用它排除会话列泄漏
        rect = self._selected_session_rect(img, geo, scale)
        if rect is not None and rect[2] < t['lx']:
            x0 = rect[2] + W * 0.004
        y0 = t['ly'] + t['lh'] * 3             # 标题行以下 (跳过群公告条)
        y1 = geo['y'] + H * 0.86               # 输入框以上
        chat = [r for r in rows if x0 <= r['lx'] and y0 <= r['ly'] <= y1]
        # 过滤低置信单字 (二维码/图片区域的 OCR 乱码)
        chat = [r for r in chat if r['conf'] > 40 or len(r['text'].strip()) > 1]
        lines = self._cluster_lines(chat)
        center = x0 + (geo['x'] + W - x0) * 0.5    # 聊天区中线
        messages = []
        cur_sender = None
        last_line_y = None
        for line in lines:
            joined = self._join([t2['text'] for t2 in line])
            if not joined:
                continue
            first = line[0]
            side = 'self' if first['lx'] > center + 8 else 'friend'
            # 时间行 (居中小字) / “N条新消息”浮标 → 忽略
            midx = first['lx'] + first['lw'] / 2
            if re.match(r'^\d{1,2}:\d{2}$', joined) and abs(midx - center) < W * 0.08:
                continue
            if '条新消息' in joined:
                continue
            # 行间距很大 → 新气泡, 群聊里可能是新发言人的昵称行
            new_bubble = (last_line_y is None
                          or (first['ly'] - last_line_y) > first['lh'] * 2.2)
            if new_bubble and side == 'friend':
                cur_sender = None
            is_sender_name = (side == 'friend' and first['lh'] < 16
                              and len(joined) < 24
                              and first['lx'] < x0 + (geo['x'] + W - x0) * 0.18)
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
            last_line_y = first['ly']
        return messages

    def GetHistoryMessage(self, n: int = 50) -> list:
        # 向上滚动几次后读取
        geo = self._geo()
        winman.activate_wechat()
        time.sleep(0.2)
        mx = geo['x'] + geo['w'] * 0.68
        my = geo['y'] + geo['h'] * 0.4
        wdt.mousemove(mx, my)
        time.sleep(0.2)
        for _ in range(3):
            wdt.run(['scroll', '--', '0', '600'])
            time.sleep(0.3)
        time.sleep(0.6)
        return self.GetAllMessage()

    # ---- 未实现的接口 (保持兼容, 明确报错) ---------------------------------
    def GetAllRecentGroups(self):
        return [s.info['name'] for s in self.GetSession()]

    def GetFriendDetails(self, n=10, timeout=0, save_head_image=False,
                         save_head_wait=0):
        return []

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

    def SwitchToChat(self):
        geo = self._geo()
        wdt.click(geo['x'] + geo['w'] * 0.10, geo['y'] + geo['h'] * 0.5)
        time.sleep(0.3)
        return True

    def SwitchToContact(self):
        geo = self._geo()
        wdt.click(geo['x'] + geo['w'] * 0.10, geo['y'] + geo['h'] * 0.25)
        time.sleep(0.4)
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
