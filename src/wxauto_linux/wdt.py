"""wdotool 封装: libei + xdg-desktop-portal 授权通道的输入注入 (逻辑坐标)。"""
import os
import subprocess
import time
from pathlib import Path

from . import winman

WDTOOL = os.environ.get(
    'WXAUTO_WDTOOL',
    str(Path.home() / 'wxauto-linux/wdotool-bin/wdotool-x86_64-unknown-linux-gnu/wdotool')
)


def _env():
    return winman.session_env()


def run(args, timeout=20):
    r = subprocess.run([WDTOOL] + args, env=_env(), timeout=timeout,
                       capture_output=True, text=True)
    return r


def search(name='微信'):
    r = run(['search', '--name', name])
    out = (r.stdout or '').strip().splitlines()
    return out[0].split('\t')[0].strip() if out else None


def activate(window_id: str | None = None, name='微信'):
    if window_id is None:
        window_id = search(name)
    if not window_id:
        return False
    r = run(['windowactivate', window_id])
    return r.returncode == 0


def mousemove(x, y, timeout=20):
    return run(['mousemove', str(int(x)), str(int(y))])


def click(x=None, y=None, button=1):
    if x is not None:
        mousemove(x, y)
        time.sleep(0.08)
    return run(['click', str(button)])


def type_text(text, delay=12000):
    # wdotool type: 文本参数
    return run(['type', '--delay', str(delay), '--', text])


def key(keys):
    """keys 形如 'ctrl+v' / 'Return' / 'ctrl+a'。"""
    return run(['key', keys])


def key_enter():
    return key('Return')


def key_escape():
    return key('Escape')


def key_ctrl_a():
    return key('ctrl+a')


def key_ctrl_v():
    return key('ctrl+v')
