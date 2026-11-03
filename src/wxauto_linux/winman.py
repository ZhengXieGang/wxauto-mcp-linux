"""窗口管理: KWin D-Bus 脚本查询/激活微信窗口 (逻辑坐标系)。"""
import json
import os
import re
import subprocess
import time

ANSI = re.compile(r'\x1b\[[0-9;]*m')

GEO_JS = (
    'const w = (() => { for (const x of workspace.windowList()) '
    'if (x.resourceClass == "wechat" && x.caption.indexOf("微信") >= 0) return x; })();'
    'if (w) { const g = w.frameGeometry; '
    'console.info("WXGEO|" + Math.round(g.x) + "|" + Math.round(g.y) + "|" '
    '+ Math.round(g.width) + "|" + Math.round(g.height)); '
    '} else { console.info("WXGEO|none"); }'
)

ACTIVATE_JS = (
    'const w = (() => { for (const x of workspace.windowList()) '
    'if (x.resourceClass == "wechat" && x.caption.indexOf("微信") >= 0) return x; })();'
    'if (w) { try { workspace.activeWindow = w; } catch (e) {} '
    'try { if (w.activate) w.activate(); } catch (e2) {} }'
)

# 激活 + 取几何合并为一次 D-Bus 往返 (原来两次脚本调用 ~1.8s, 现在 ~0.7s)
ACTIVATE_GEO_JS = (
    'const w = (() => { for (const x of workspace.windowList()) '
    'if (x.resourceClass == "wechat" && x.caption.indexOf("微信") >= 0) return x; })();'
    'if (!w) { console.info("WXGA|none"); } else { '
    'let act = "n"; '
    'try { if (workspace.activeWindow !== w) { '
    'try { workspace.activeWindow = w; } catch (e) {} '
    'try { if (w.activate) w.activate(); } catch (e2) {} act = "y"; } } catch (e3) {} '
    'const g = w.frameGeometry; '
    'console.info("WXGA|" + act + "|" + Math.round(g.x) + "|" + Math.round(g.y) + "|" '
    '+ Math.round(g.width) + "|" + Math.round(g.height)); }'
)


def session_env():
    """完整图形会话环境: MCP 宿主会过滤 WAYLAND_DISPLAY/DBUS 等变量, 这里推断补齐。"""
    import glob
    e = dict(os.environ)
    xrd = e.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    e.setdefault('XDG_RUNTIME_DIR', xrd)
    e.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={xrd}/bus')
    if not e.get('WAYLAND_DISPLAY') and not e.get('DISPLAY'):
        cands = sorted(glob.glob(f'{xrd}/wayland-*'))
        e['WAYLAND_DISPLAY'] = cands[0].rsplit('/', 1)[-1] if cands else 'wayland-0'
    e.setdefault('XDG_CURRENT_DESKTOP', 'KDE')
    return e


def _env():
    return session_env()


def kwin_run_js(js: str, tag: str, wait=0.5):
    """运行一段 KWin 脚本并从 journal 抓取 tag| 开头的输出。"""
    import tempfile
    tf = tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False)
    tf.write(js)
    tf.close()
    # loadScript 空 pluginName 会返回 -1, 必须用显式唯一名字
    pname = f'wxa{tag}{int(time.time()*1000) % 1000000}'
    r = subprocess.run(['qdbus', 'org.kde.KWin', '/Scripting',
                        'org.kde.kwin.Scripting.loadScript', tf.name, pname],
                       capture_output=True, text=True, env=_env(), timeout=10)
    sid = (r.stdout or '').strip()
    if not sid.isdigit():
        try:
            import os
            os.unlink(tf.name)
        except Exception:
            pass
        return None
    time.sleep(0.15)
    subprocess.run(['qdbus', 'org.kde.KWin', f'/Scripting/Script{sid}',
                    'org.kde.kwin.Script.run'], capture_output=True, env=_env(), timeout=10)
    # 脚本队列偶发卡住, start() 冲刷队列
    subprocess.run(['qdbus', 'org.kde.KWin', '/Scripting',
                    'org.kde.kwin.Scripting.start'], capture_output=True, env=_env(), timeout=10)
    time.sleep(wait)
    # 先读 journal 再 stop/unload: stop 会丢弃尚未落盘的 console 输出
    out = subprocess.run(['journalctl', '--user', '--since', '20 seconds ago',
                          '--no-pager'], capture_output=True, text=True,
                         env=_env(), timeout=10).stdout
    try:
        subprocess.run(['qdbus', 'org.kde.KWin', f'/Scripting/Script{sid}',
                        'org.kde.kwin.Script.stop'], capture_output=True, env=_env(), timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(['qdbus', 'org.kde.KWin', '/Scripting',
                        'org.kde.kwin.Scripting.unloadScript', pname],
                       capture_output=True, env=_env(), timeout=5)
    except Exception:
        pass
    lines = [l for l in out.splitlines() if f'{tag}|' in l]
    try:
        import os
        os.unlink(tf.name)
    except Exception:
        pass
    if not lines:
        return None
    return lines[-1].split(f'{tag}|', 1)[1]


def desktop_logical_size(for_geo=None):
    """显示器逻辑尺寸与缩放。

    for_geo: 传入窗口几何时, 选择包含窗口中心的那块屏 —— 多显示器且
    混合 DPI (如外接 1.0 + 内屏 1.5) 时, 必须用窗口所在屏的 scale,
    否则截图裁剪坐标全部错位。无匹配时退回最大面积的屏。
    """
    r = subprocess.run(['kscreen-doctor', '-o'], capture_output=True, text=True,
                       env=_env(), timeout=10)
    clean = ANSI.sub('', r.stdout)
    outs = []
    for blk in re.split(r'Output:\s*\d+\s+\S+', clean)[1:]:
        g = re.search(r'Geometry:\s*(\d+),(\d+)\s+(\d+)x(\d+)', blk)
        s = re.search(r'Scale:\s*([\d.]+)', blk)
        if not g:
            continue
        outs.append({'x': int(g.group(1)), 'y': int(g.group(2)),
                     'w': int(g.group(3)), 'h': int(g.group(4)),
                     'scale': float(s.group(1)) if s else 1.0})
    if not outs:
        return {'w': 0, 'h': 0, 'scale': 1.0}
    pick = outs[0]
    if for_geo is not None and len(outs) > 1:
        cx = for_geo['x'] + for_geo['w'] / 2
        cy = for_geo['y'] + for_geo['h'] / 2
        for o in outs:
            if o['x'] <= cx < o['x'] + o['w'] and o['y'] <= cy < o['y'] + o['h']:
                pick = o
                break
    return {'w': pick['x'] + pick['w'], 'h': pick['y'] + pick['h'],
            'scale': pick['scale']}


def get_wechat_geometry() -> dict | None:
    out = kwin_run_js(GEO_JS, 'WXGEO')
    if not out or out == 'none':
        return None
    parts = out.split('|')
    return {'x': float(parts[0]), 'y': float(parts[1]),
            'w': float(parts[2]), 'h': float(parts[3])}


def activate_wechat() -> bool:
    kwin_run_js(ACTIVATE_JS, 'WXACT', wait=0.4)
    return True


def activate_and_geo() -> tuple:
    """激活微信窗口并返回几何, 一次 D-Bus 往返。

    返回 (geo, activated): activated=False 表示窗口本就在前台 (可少等一会儿)。
    """
    out = kwin_run_js(ACTIVATE_GEO_JS, 'WXGA', wait=0.35)
    if not out or out == 'none':
        return None, False
    p = out.split('|')          # [act, x, y, w, h]
    if len(p) < 5:
        return get_wechat_geometry(), True
    return ({'x': float(p[1]), 'y': float(p[2]),
             'w': float(p[3]), 'h': float(p[4])}, p[0] == 'y')


def restore_wechat_from_tray() -> bool:
    """微信窗口被关闭到托盘时, 通过 StatusNotifierItem Activate 恢复。"""
    try:
        r = subprocess.run(['busctl', '--user', 'list'], capture_output=True,
                           text=True, env=_env(), timeout=10)
        names = set(re.findall(r'org\.kde\.StatusNotifierItem-\d+-\d+', r.stdout or ''))
    except Exception:
        return False
    for name in sorted(names):
        try:
            rid = subprocess.run(['qdbus', name, '/StatusNotifierItem',
                                  'org.kde.StatusNotifierItem.Id'],
                                 capture_output=True, text=True, env=_env(),
                                 timeout=5).stdout.strip()
        except Exception:
            continue
        if rid != 'wechat':
            continue
        try:
            subprocess.run(['qdbus', name, '/StatusNotifierItem',
                            'org.kde.StatusNotifierItem.Activate', '0', '0'],
                           capture_output=True, env=_env(), timeout=5)
            time.sleep(1.2)
            return get_wechat_geometry() is not None
        except Exception:
            continue
    return False
