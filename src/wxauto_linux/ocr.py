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
    # spectacle 异步写盘: 等文件大小连续两次采样稳定即认为写完
    # (旧版固定 sleep(0.9), 每张截图白等 ~0.6s)
    last = -1
    ok = False
    for _ in range(40):
        try:
            sz = os.path.getsize(path)
        except OSError:
            sz = -1
        if sz > 1000 and sz == last:
            ok = True
            break
        last = sz
        time.sleep(0.12)
    if not ok:
        raise RuntimeError('截图失败')
    return path


def window_shot(wait_activate=True, shrink=0, geo=None):
    """返回微信窗口区域的 PIL Image (物理像素)。

    geo: 已知的窗口几何 (避免重复查询 KWin); 传 None 则自查。
    """
    from PIL import Image
    if wait_activate:
        winman.activate_wechat()
        time.sleep(0.35)
    if geo is None:
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
