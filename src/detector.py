"""二维码 / 一维条码检测模块。

后端优先级：zxing-cpp → zbar → OpenCV。

- zxing-cpp：支持 QR / 全部常见一维码 / DataMatrix / Aztec / MaxiCode，给出四角坐标，优先使用；
- zbar（pyzbar）：zxing-cpp 不可用时兜底，支持 QR 与常见一维码；
- OpenCV 自带检测器：两者都不可用时的最后兜底。

性能策略：先缩放到 DETECT_MAX_WIDTH 宽再灰度化解码，解码坐标再按比例还原到
原始帧坐标系，因此对外返回的 Code 始终使用原始帧坐标。720p 下解码耗时约降一半。

对外暴露 Code 数据结构与 CodeDetector.detect()；DetectionWorker 负责把「取最新帧 →
解码」这件事跑在独立线程里，避免拖慢采集与界面。
"""

import time
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

try:
    import zxingcpp

    _HAS_ZXING = True
except Exception:  # zxing-cpp 未安装
    _HAS_ZXING = False

try:
    from pyzbar import pyzbar

    _HAS_ZBAR = True
except Exception:  # pyzbar / libzbar 不可用
    _HAS_ZBAR = False

# OpenCV 会为每次 resize/cvtColor 拉起内部线程池，板端 4 核上这些 worker 加起来
# 反而比单线程更吃 CPU。固定单线程后总占用明显下降，单帧解码只慢几毫秒。
try:
    cv2.setNumThreads(1)
except Exception:
    pass


# 解码前的最大宽度：720p 缩到 960 宽，像素量约减半，识别率基本不变
DETECT_MAX_WIDTH = 960

# 两次解码之间至少间隔这么久（秒）。板端 960 宽单帧解码约 28ms，不节流会一直
# 吃满一个核；限制在 20fps 左右，锁定动画和手感都看不出差别，但 CPU 省一半。
DETECT_MIN_INTERVAL = 0.045


@dataclass
class Code:
    """识别出的一个码。"""

    data: str          # 解码出的文本内容
    symbology: str     # 归一化后的码类型，如 QR Code / EAN-13 / Code 128
    center: tuple      # 中心点 (x, y)，原图坐标系
    polygon: list      # 外框四个角点 [(x, y), ...]，原图坐标系


# 各后端返回的码制名不统一（QRCode / QR-Code / QRCODE），统一成展示用名称
_SYMBOLOGY_ALIASES = {
    "QRCODE": "QR Code",
    "MICROQRCODE": "Micro QR",
    "RMQRCODE": "rMQR",
    "DATAMATRIX": "Data Matrix",
    "PDF417": "PDF417",
    "AZTEC": "Aztec",
    "MAXICODE": "MaxiCode",
    "EAN13": "EAN-13",
    "EAN8": "EAN-8",
    "EAN2": "EAN-2",
    "EAN5": "EAN-5",
    "UPCA": "UPC-A",
    "UPCE": "UPC-E",
    "CODE39": "Code 39",
    "CODE93": "Code 93",
    "CODE128": "Code 128",
    "CODABAR": "Codabar",
    "ITF": "ITF",
    "ISBN": "ISBN",
    "NONE": "Unknown",
}


def normalize_symbology(name: str) -> str:
    key = "".join(ch for ch in (name or "").upper() if ch.isalnum())
    if not key:
        return "Unknown"
    if key in _SYMBOLOGY_ALIASES:
        return _SYMBOLOGY_ALIASES[key]
    return name


class CodeDetector:
    def __init__(self) -> None:
        if _HAS_ZXING:
            self.backend = "zxing"
        elif _HAS_ZBAR:
            self.backend = "zbar"
        else:
            self.backend = "opencv"

        self._qr = None
        self._bar = None
        if self.backend == "opencv":
            self._qr = cv2.QRCodeDetector()
            try:
                self._bar = cv2.barcode.BarcodeDetector()
            except Exception:
                self._bar = None

    def detect(self, frame_bgr: np.ndarray) -> List[Code]:
        """在 BGR 图像中检测所有码，返回去重后的结果列表（坐标为原图坐标）。"""
        h, w = frame_bgr.shape[:2]
        scale = 1.0
        work = frame_bgr
        if w > DETECT_MAX_WIDTH:
            scale = DETECT_MAX_WIDTH / float(w)
            work = cv2.resize(frame_bgr, (DETECT_MAX_WIDTH, max(1, int(h * scale))),
                              interpolation=cv2.INTER_AREA)

        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        if self.backend == "zxing":
            codes = self._detect_zxing(gray)
        elif self.backend == "zbar":
            codes = self._detect_zbar(gray)
        else:
            codes = self._detect_opencv(gray)

        if scale != 1.0:
            inv = 1.0 / scale
            for c in codes:
                c.polygon = [(x * inv, y * inv) for (x, y) in c.polygon]
                c.center = (c.center[0] * inv, c.center[1] * inv)

        # 同一内容只保留一个，避免同一码重复触发
        seen = {}
        for c in codes:
            seen.setdefault(c.data, c)
        return list(seen.values())

    # ---------------- 后端实现 ----------------

    def _detect_zxing(self, gray: np.ndarray) -> List[Code]:
        codes: List[Code] = []
        for r in zxingcpp.read_barcodes(gray):
            try:
                pos = r.position
            except Exception:
                pos = None
            if pos is None or pos.top_left is None:
                continue
            poly = [
                (float(pos.top_left.x), float(pos.top_left.y)),
                (float(pos.top_right.x), float(pos.top_right.y)),
                (float(pos.bottom_right.x), float(pos.bottom_right.y)),
                (float(pos.bottom_left.x), float(pos.bottom_left.y)),
            ]
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            try:
                symbology = normalize_symbology(r.format.name)
            except Exception:
                symbology = "Unknown"
            codes.append(Code(data=r.text, symbology=symbology,
                              center=(cx, cy), polygon=poly))
        return codes

    def _detect_zbar(self, gray: np.ndarray) -> List[Code]:
        codes: List[Code] = []
        for r in pyzbar.decode(gray):
            if not r.polygon:
                continue
            poly = [(float(p.x), float(p.y)) for p in r.polygon]
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            data = r.data.decode("utf-8", errors="replace")
            codes.append(Code(data=data, symbology=normalize_symbology(r.type),
                              center=(cx, cy), polygon=poly))
        return codes

    def _detect_opencv(self, gray: np.ndarray) -> List[Code]:
        codes: List[Code] = []

        try:
            ok, infos, points, _ = self._qr.detectAndDecodeMulti(gray)
            if ok and points is not None:
                for info, pts in zip(infos, points):
                    if not info:
                        continue
                    poly = [(float(x), float(y)) for x, y in pts]
                    cx = sum(p[0] for p in poly) / len(poly)
                    cy = sum(p[1] for p in poly) / len(poly)
                    codes.append(Code(data=info, symbology="QR Code", center=(cx, cy), polygon=poly))
        except Exception:
            pass

        if self._bar is not None and not codes:
            try:
                retval, decoded, decoded_type, points = self._bar.detectAndDecodeWithType(gray)
                if retval and decoded and points is not None:
                    for info, pts in zip(decoded, points):
                        if not info:
                            continue
                        poly = [(float(x), float(y)) for x, y in pts]
                        cx = sum(p[0] for p in poly) / len(poly)
                        cy = sum(p[1] for p in poly) / len(poly)
                        codes.append(Code(data=info, symbology=normalize_symbology(decoded_type or "BARCODE"),
                                          center=(cx, cy), polygon=poly))
            except Exception:
                pass

        return codes


class DetectionWorker(QThread):
    """独立解码线程：只处理摄像头最新的那一帧，处理不过来就自然丢帧。"""

    detections = Signal(int, object, float)   # (generation, List[Code], 耗时 ms)

    def __init__(self, generation: int, camera, detector: CodeDetector, parent=None) -> None:
        super().__init__(parent)
        self.generation = generation
        self.camera = camera
        self.detector = detector
        self._running = True

    def request_stop(self) -> None:
        self._running = False

    def run(self) -> None:
        last_seq = -1
        while self._running:
            try:
                frame, seq = self.camera.latest()
            except RuntimeError:
                # 摄像头对象已随会话结束被销毁
                return
            if frame is None or seq == last_seq:
                self.msleep(4)
                continue
            last_seq = seq

            started = time.perf_counter()
            try:
                codes = self.detector.detect(frame)
            except Exception:
                codes = []
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            if self._running:
                self.detections.emit(self.generation, codes, elapsed_ms)

            spent = time.perf_counter() - started
            wait = DETECT_MIN_INTERVAL - spent
            if wait > 0:
                self.msleep(int(wait * 1000))
