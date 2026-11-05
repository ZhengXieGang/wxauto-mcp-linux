"""文件/图片剪贴板: 直接实现 ext-data-control-v1 Wayland 协议。

为什么不用 wl-copy: 它一次只能挂一个 MIME 类型, 而微信 Linux 只在
剪贴板同时带有 image/* (或 uri-list + gnome-copied-files) 时才把
粘贴内容当作图片/文件处理 —— 单类型会被当成纯文本。

实测结论 (微信 Linux 输入框的粘贴处理器只有两个分支):
- image/png 等图片 MIME → 出图片卡片, 可发送
- 文件 (uri-list) → 没有对应分支, 永远退化成纯文本路径
所以本模块对图片走粘贴路线; 普通文件必须走「发送文件」对话框 (wechat.py)。

用法 (驱动内为子进程; 也可手动测试):
  CLIP_LIVE=12 python3 -m wxauto_linux.clip <file...>
进程存活 CLIP_LIVE 秒为粘贴方提供数据, 之后自动退出。
"""
import os
import sys
import time

from pywayland.client import Display
from pywayland.protocol.wayland import WlSeat

if __package__:
    from ._wlp.ext_data_control_v1 import ExtDataControlManagerV1
else:
    # 作为独立脚本运行 (驱动以子进程方式调起): 补包路径后用绝对导入
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from wxauto_linux._wlp.ext_data_control_v1 import ExtDataControlManagerV1

_IMG_MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
             '.gif': 'image/gif', '.bmp': 'image/bmp', '.webp': 'image/webp'}


def build_mimes(paths):
    uris = [f'file://{p}' for p in paths]
    text = '\n'.join(paths).encode()
    mimes = {
        'x-special/gnome-copied-files': ('copy\n' + '\n'.join(uris)).encode(),
        'text/uri-list': ('\r\n'.join(uris) + '\r\n').encode(),
        'text/plain;charset=utf-8': text,
        'text/plain': text,
        'UTF8_STRING': text,
    }
    if paths:
        m = _IMG_MIME.get(os.path.splitext(paths[0])[1].lower())
        if m:
            mimes[m] = open(paths[0], 'rb').read()
    return mimes


def serve(paths, live=12.0):
    """把 paths 挂上剪贴板并存活 live 秒。成功返回 True。"""
    mimes = build_mimes(paths)
    state = {'seat': None, 'mgr': None, 'served': False}

    def registry_global(reg, name, iface, version):
        if iface == 'wl_seat':
            state['seat'] = reg.bind(name, WlSeat, version)
        elif iface == 'ext_data_control_manager_v1':
            state['mgr'] = reg.bind(name, ExtDataControlManagerV1, min(version, 2))

    def on_send(_src, mime, fd):
        # ext 协议 send 事件参数顺序是 (mime_type, fd), 与 wlr 版相反
        data = mimes.get(mime, b'')
        try:
            while data:
                n = os.write(fd, data)
                data = data[n:]
        finally:
            os.close(fd)
        state['served'] = True

    display = Display()
    display.connect()
    reg = display.get_registry()
    reg.dispatcher['global'] = registry_global
    display.roundtrip()
    display.roundtrip()
    if state['mgr'] is None or state['seat'] is None:
        return False
    dev = state['mgr'].get_data_device(state['seat'])
    src = state['mgr'].create_data_source()
    for m in mimes:
        src.offer(m)
    src.dispatcher['send'] = on_send
    dev.set_selection(src)
    display.flush()
    print('READY', flush=True)
    end = time.time() + live
    while time.time() < end:
        # roundtrip 才会真正从 socket 读事件 (dispatch(block=False) 只处理
        # 已入队事件, send 请求永远收不到 → 粘贴方拿到空数据)
        try:
            display.roundtrip()
        except Exception:
            break
        time.sleep(0.03)
    try:
        src.destroy()
    except Exception:
        pass
    display.disconnect()
    return True


if __name__ == '__main__':
    live = float(os.environ.get('CLIP_LIVE', '12'))
    ok = serve([os.path.abspath(p) for p in sys.argv[1:]], live)
    sys.exit(0 if ok else 1)
