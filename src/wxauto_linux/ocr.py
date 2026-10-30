"""截图 + OCR: 从 KWin 窗口几何裁剪全屏截图, tesseract(chi_sim) 识别。"""
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import winman

TESSDATA = os.environ.get(
    'WXAUTO_TESSDATA', str(Path.home() / '.local/share/tessdata'))


def full_screenshot(path=None):
    path = path or tempfile.mktemp(prefix='wxshot_', suffix='.png', dir='/tmp')
    subprocess.run(['spectacle', '-b', '-n', '-o', path], timeout=25,
                   capture_output=True, env=winman.session_env())
    for _ in range(30):
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            time.sleep(0.9)   # spectacle 异步写盘, 等待完成
            return path
        time.sleep(0.1)
    raise RuntimeError('截图失败')


def window_shot(wait_activate=True, shrink=0):
    """返回微信窗口区域的 PIL Image (物理像素)。"""
    from PIL import Image
    if wait_activate:
        winman.activate_wechat()
        time.sleep(0.35)
    geo = winman.get_wechat_geometry()
    if geo is None:
        raise RuntimeError('未找到微信窗口')
    scale = winman.desktop_logical_size().get('scale', 1.0)
    path = full_screenshot()
    im = Image.open(path)
    x, y = int(geo['x'] * scale), int(geo['y'] * scale)
    w, h = int(geo['w'] * scale), int(geo['h'] * scale)
    box = (x + shrink, y + shrink, x + w - shrink, y + h - shrink)
    crop = im.crop(box)
    os.unlink(path)
    dbg = os.environ.get('WXAUTO_DEBUG_SHOT')
    if dbg:
        try:
            n = getattr(window_shot, '_n', 0) + 1
            window_shot._n = n
            crop.save(f'{dbg}.{n}.png')
        except Exception:
            pass
    return crop, geo, scale


def ocr(image, lang='chi_sim', psm=3, tsv=True) -> list[dict]:
    """OCR 返回 [{text, x, y, w, h, conf}] (图像内坐标)。psm3 (自动版面) 比 psm6
    对深色 UI 上的浅色占位文字 (如搜索框「搜索」) 稳定得多。"""
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        image.save(f.name)
        src = f.name
    try:
        cmd = ['tesseract', src, 'stdout', '-l', lang, '--psm', str(psm)]
        env = {**os.environ, 'TESSDATA_PREFIX': TESSDATA}
        if tsv:
            cmd.append('tsv')
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            rows = []
            for line in (r.stdout or '').splitlines()[1:]:
                parts = line.split('\t')
                if len(parts) >= 12:
                    try:
                        conf = float(parts[10])
                    except ValueError:
                        continue
                    text = parts[11].strip()
                    if not text:
                        continue
                    rows.append({'text': text, 'x': int(parts[6]), 'y': int(parts[7]),
                                 'w': int(parts[8]), 'h': int(parts[9]), 'conf': conf})
            return rows
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        return [{'text': l, 'x': 0, 'y': 0, 'w': 0, 'h': 0, 'conf': 0}
                for l in (r.stdout or '').splitlines() if l.strip()]
    finally:
        os.unlink(src)
