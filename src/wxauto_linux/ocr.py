"""截图 + OCR。

引擎: PP-OCRv6 (rapidocr, onnxruntime CPU) 为主, tesseract chi_sim 兜底。
关键坑:
- PaddleOCR 系模型按 OpenCV 惯例吃 BGR —— 喂 RGB 时深色 UI 检测会整块崩掉
- 自己消息是「深色字+绿底」, 检测模型漏检 → 绿掩码找框 + 深色像素定文字
  bbox + 原样紧裁 + rec-only (二值化反而更差, v6 是自然图像模型)
- 搜索框灰色占位字 (「搜索」) 对比度过低漏检 → 灰度 levels 拉伸后再 OCR
"""
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import winman

TESSDATA = os.environ.get(
    'WXAUTO_TESSDATA', str(Path.home() / '.local/share/tessdata'))

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import RapidOCR
                # use_cls=False: 界面文字无旋转, 省掉每行的方向分类 (~30% 提速)
                _engine = RapidOCR(params={'Global.use_cls': False})
    return _engine


def _to_bgr(image):
    import numpy as np
    a = np.asarray(image.convert('RGB'))
    return np.ascontiguousarray(a[:, :, ::-1])


def ocr(image, lang='chi_sim', psm=3, tsv=True) -> list[dict]:
    """OCR 返回 [{text, x, y, w, h, conf}] (图像内坐标, conf 0-100)。

    psm 参数仅为 tesseract 兼容保留 (rapidocr 忽略)。
    """
    try:
        return _ocr_rapid(image)
    except Exception:
        if os.environ.get('WXAUTO_DISABLE_TESSERACT'):
            raise
        return _ocr_tesseract(image, lang, psm, tsv)


def _ocr_rapid(image) -> list[dict]:
    eng = _get_engine()
    r = eng(_to_bgr(image))
    boxes = r.boxes if r.boxes is not None else []
    txts = r.txts if r.txts is not None else []
    scores = r.scores if r.scores is not None else []
    rows = []
    for b, t, s in zip(boxes, txts, scores):
        xs = [p[0] for p in b]
        ys = [p[1] for p in b]
        rows.append({'text': str(t), 'x': int(min(xs)), 'y': int(min(ys)),
                     'w': int(max(xs) - min(xs)), 'h': int(max(ys) - min(ys)),
                     'conf': float(s) * 100})
    return rows


def _ocr_tesseract(image, lang='chi_sim', psm=3, tsv=True) -> list[dict]:
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


def ocr_rec_line(image) -> list[dict]:
    """识别模式 (跳过检测): 对已裁紧的单行文字图直接识别。
    返回 [{text, conf}]。用于绿气泡文字 bbox (检测模型对深字绿底漏检)。"""
    eng = _get_engine()
    r = eng(_to_bgr(image), use_det=False, use_cls=False)
    txts = r.txts if r.txts is not None else []
    scores = r.scores if r.scores is not None else []
    return [{'text': str(t), 'conf': float(s) * 100} for t, s in zip(txts, scores)]


def stretch_gray(image, lo=35, hi=150):
    """灰度 levels 拉伸 (暗色 UI 上的低对比占位字 → 正常对比)。"""
    from PIL import Image
    import numpy as np
    a = np.asarray(image.convert('L')).astype(float)
    return Image.fromarray(np.clip((a - lo) / max(1, hi - lo) * 255, 0, 255).astype('uint8'))


def full_screenshot(path=None):
    path = path or tempfile.mktemp(prefix='wxshot_', suffix='.png', dir='/tmp')
    subprocess.run(['spectacle', '-b', '-n', '-o', path], timeout=25,
                   capture_output=True, env=winman.session_env())
    # spectacle 异步写盘: 等文件大小连续两次采样稳定即认为写完
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
    # 窗口所在屏的 scale (多屏混合 DPI 时不能用第一块屏的)
    scale = winman.desktop_logical_size(geo).get('scale', 1.0)
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
