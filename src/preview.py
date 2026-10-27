"""ScannerView：取景画布 + HUD 叠加层。

负责把摄像头画面画满控件，并在上面叠加工业读码器的视觉元素：
- 扫描线 / 中央准星（待机与无码时）
- 每个码的四角锁定框，边框会按“稳定帧进度”绕轮廓逐步点亮
- 码制标签、左上角运行读数、识别成功时的白色闪光
不做任何业务判断，稳定帧/入架逻辑都在 main.py。
"""

import math

import cv2
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

BG = QColor("#05080d")
ACCENT = QColor("#2ee6a8")
TEXT = QColor("#e6f1ff")
DIM = QColor("#7b93a8")

DISPLAY_MAX_WIDTH = 1280   # 显示用的最大宽度，超过则先缩小，减少每帧转换开销


def _mono(size: int, bold: bool = False) -> QFont:
    font = QFont("monospace")
    font.setStyleHint(QFont.Monospace)
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def _toward(a: QPointF, b: QPointF, length: float) -> QPointF:
    dx, dy = b.x() - a.x(), b.y() - a.y()
    dist = math.hypot(dx, dy) or 1.0
    return QPointF(a.x() + dx / dist * length, a.y() + dy / dist * length)


class _Track:
    """一个码在画面上的跟踪状态。"""

    __slots__ = ("data", "symbology", "poly", "target", "progress", "target_progress", "flash", "miss")

    def __init__(self, data: str, symbology: str, poly, progress: float) -> None:
        self.data = data
        self.symbology = symbology
        self.poly = list(poly)
        self.target = list(poly)
        self.progress = progress
        self.target_progress = progress
        self.flash = 0.0
        self.miss = 0


class ScannerView(QWidget):
    codeClicked = Signal(str)   # 点击了某个锁定框
    emptyClicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("scannerView")
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)

        self._image = None
        self._img_size = (0, 0)
        self._tracks: dict[str, _Track] = {}
        self._live = False
        self._phase = 0.0
        self._flash = 0.0
        self._readout = ""
        self._hint = ""
        self._idle_text = "CAMERA IDLE"

        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._dw = 0.0
        self._dh = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---------------- 对外接口 ----------------

    def set_frame(self, frame_bgr) -> None:
        h, w = frame_bgr.shape[:2]
        if w > DISPLAY_MAX_WIDTH:
            scale = DISPLAY_MAX_WIDTH / float(w)
            frame_bgr = cv2.resize(frame_bgr, (DISPLAY_MAX_WIDTH, max(1, int(h * scale))),
                                   interpolation=cv2.INTER_AREA)
            h, w = frame_bgr.shape[:2]

        # 直接用 Qt 的原生 32 位格式（BGRA），省掉绘制时 Qt 每次再做一遍
        # RGB888 -> ARGB32 的整帧转换，板端这一项能吃掉近一个核
        bgra = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2BGRA)
        self._image = QImage(bgra.data, w, h, bgra.strides[0], QImage.Format_RGB32).copy()
        self._img_size = (w, h)

    def clear_frame(self) -> None:
        self._image = None
        self._img_size = (0, 0)
        self._tracks.clear()

    def set_live(self, live: bool) -> None:
        self._live = live

    def set_codes(self, codes, progress: dict) -> None:
        """更新跟踪框。progress: {内容: 0..1 的稳定度}。"""
        targets = {c.data: c for c in codes}

        for data in list(self._tracks):
            if data in targets:
                continue
            track = self._tracks[data]
            # 短暂保留，避免检测偶发抖动时框子闪烁
            track.miss += 1
            track.target_progress = 0.0
            if track.miss > 3:
                del self._tracks[data]

        for data, code in targets.items():
            poly = code.polygon if len(code.polygon) >= 3 else self._rect_polygon(code.center)
            track = self._tracks.get(data)
            if track is None:
                # 新码从中心“弹开”，形成锁定动效
                cx = sum(p[0] for p in poly) / len(poly)
                cy = sum(p[1] for p in poly) / len(poly)
                start = [(cx + (x - cx) * 0.45, cy + (y - cy) * 0.45) for (x, y) in poly]
                track = _Track(data, code.symbology, start, 0.0)
                self._tracks[data] = track
            track.miss = 0
            track.target = poly
            track.symbology = code.symbology
            track.target_progress = float(progress.get(data, 0.0))

    def clear_codes(self) -> None:
        self._tracks.clear()

    def trigger_accept(self, data: str) -> None:
        self._flash = 1.0
        track = self._tracks.get(data)
        if track is not None:
            track.flash = 1.0

    def set_readout(self, text: str) -> None:
        self._readout = text

    def set_hint(self, text: str) -> None:
        self._hint = text

    def set_idle_text(self, text: str) -> None:
        self._idle_text = text

    @staticmethod
    def _rect_polygon(center) -> list:
        cx, cy = center
        r = 40
        return [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]

    # ---------------- 动画 ----------------

    def _tick(self) -> None:
        self._phase = (self._phase + 0.012) % 1.0
        if self._flash > 0:
            self._flash = max(0.0, self._flash - 0.07)

        for track in self._tracks.values():
            if len(track.poly) == len(track.target):
                track.poly = [(x + (tx - x) * 0.35, y + (ty - y) * 0.35)
                              for (x, y), (tx, ty) in zip(track.poly, track.target)]
            else:
                track.poly = list(track.target)
            track.progress += (track.target_progress - track.progress) * 0.3
            if track.flash > 0:
                track.flash = max(0.0, track.flash - 0.07)

        self.update()

    # ---------------- 坐标映射 ----------------

    def _compute_mapping(self) -> None:
        w, h = self.width(), self.height()
        iw, ih = self._img_size
        if iw == 0 or ih == 0:
            self._scale, self._ox, self._oy, self._dw, self._dh = 1.0, 0.0, 0.0, 0.0, 0.0
            return
        self._scale = min(w / iw, h / ih)
        self._dw = iw * self._scale
        self._dh = ih * self._scale
        self._ox = (w - self._dw) / 2
        self._oy = (h - self._dh) / 2

    def _to_screen(self, point) -> QPointF:
        return QPointF(self._ox + point[0] * self._scale, self._oy + point[1] * self._scale)

    # ---------------- 绘制 ----------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), BG)
        self._compute_mapping()

        if self._image is None:
            self._paint_idle(painter)
            return

        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = QRectF(self._ox, self._oy, self._dw, self._dh)
        painter.drawImage(rect, self._image)

        self._paint_grid(painter, rect)
        if self._live:
            self._paint_scanline(painter, rect)
            self._paint_reticle(painter, rect)
        self._paint_vignette(painter, rect)
        for track in self._tracks.values():
            self._paint_track(painter, track)
        self._paint_readout(painter)
        self._paint_flash(painter, rect)
        self._paint_hint(painter)

    def _paint_idle(self, painter: QPainter) -> None:
        rect = QRectF(self.rect())
        box_w = min(rect.width() * 0.52, 520)
        box_h = min(rect.height() * 0.42, 300)
        box = QRectF(rect.center().x() - box_w / 2, rect.center().y() - box_h / 2, box_w, box_h)

        painter.setPen(QPen(QColor(46, 230, 168, 55), 1.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(box, 12, 12)
        self._draw_corners(painter, [box.topLeft(), box.topRight(), box.bottomRight(), box.bottomLeft()],
                           QColor(46, 230, 168, 150), 3.0, 20)

        if self._live:
            y = box.top() + self._phase * box.height()
            gradient = QLinearGradient(0, y - 70, 0, y)
            gradient.setColorAt(0.0, QColor(46, 230, 168, 0))
            gradient.setColorAt(1.0, QColor(46, 230, 168, 30))
            painter.fillRect(QRectF(box.left(), y - 70, box.width(), 70), gradient)
            painter.setPen(QPen(QColor(46, 230, 168, 90), 1.2))
            painter.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))

        painter.setPen(QColor("#5d7488"))
        painter.setFont(_mono(12, True))
        painter.drawText(box, Qt.AlignCenter, self._idle_text)

        painter.setFont(_mono(10))
        painter.setPen(QColor("#43596d"))
        painter.drawText(QRectF(0, box.bottom() + 14, rect.width(), 20),
                         Qt.AlignHCenter | Qt.AlignTop,
                         "SPACE  start / stop      D  diagnostics")

    def _paint_grid(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(QColor(255, 255, 255, 11), 1))
        step = 64
        x = rect.left() + step / 2
        while x < rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        y = rect.top() + step / 2
        while y < rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step

    def _paint_scanline(self, painter: QPainter, rect: QRectF) -> None:
        y = rect.top() + self._phase * rect.height()
        gradient = QLinearGradient(0, y - 110, 0, y)
        gradient.setColorAt(0.0, QColor(46, 230, 168, 0))
        gradient.setColorAt(1.0, QColor(46, 230, 168, 18))
        painter.fillRect(QRectF(rect.left(), y - 110, rect.width(), 110), gradient)
        painter.setPen(QPen(QColor(46, 230, 168, 70), 1.2))
        painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    def _paint_reticle(self, painter: QPainter, rect: QRectF) -> None:
        if self._tracks:
            return
        box_w = rect.width() * 0.56
        box_h = rect.height() * 0.48
        box = QRectF(rect.center().x() - box_w / 2, rect.center().y() - box_h / 2, box_w, box_h)
        painter.setPen(QPen(QColor(46, 230, 168, 45), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(box, 10, 10)
        self._draw_corners(painter, [box.topLeft(), box.topRight(), box.bottomRight(), box.bottomLeft()],
                           QColor(46, 230, 168, 120), 2.4, 18)

    def _paint_vignette(self, painter: QPainter, rect: QRectF) -> None:
        gradient = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * 0.72)
        gradient.setColorAt(0.55, QColor(0, 0, 0, 0))
        gradient.setColorAt(1.0, QColor(0, 0, 0, 120))
        painter.fillRect(rect, gradient)

    def _paint_track(self, painter: QPainter, track: _Track) -> None:
        if len(track.poly) < 3:
            return
        points = [self._to_screen(p) for p in track.poly]
        polygon = QPolygonF(points)

        painter.setPen(QPen(QColor(200, 255, 235, 55), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(polygon)

        self._draw_progress_outline(painter, points, track.progress)
        if track.progress > 0.5:
            self._draw_corners(painter, points, QColor(46, 230, 168, 210), 3.0, 16)

        self._draw_label(painter, points, track.symbology)

        if track.flash > 0:
            painter.setPen(QPen(QColor(255, 255, 255, int(200 * track.flash)), 3.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(polygon)

    def _draw_progress_outline(self, painter: QPainter, points, progress: float) -> None:
        if progress <= 0.01:
            return
        total = 0.0
        n = len(points)
        for i in range(n):
            a, b = points[i], points[(i + 1) % n]
            total += math.hypot(b.x() - a.x(), b.y() - a.y())
        budget = total * min(progress, 1.0)

        path = QPainterPath()
        started = False
        for i in range(n):
            a, b = points[i], points[(i + 1) % n]
            seg = math.hypot(b.x() - a.x(), b.y() - a.y())
            if budget <= 0:
                break
            if not started:
                path.moveTo(a)
                started = True
            if seg <= budget:
                path.lineTo(b)
                budget -= seg
            else:
                k = budget / seg if seg else 0.0
                path.lineTo(QPointF(a.x() + (b.x() - a.x()) * k, a.y() + (b.y() - a.y()) * k))
                budget = 0

        painter.setPen(QPen(ACCENT, 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _draw_corners(self, painter: QPainter, points, color: QColor, width: float, length: float) -> None:
        n = len(points)
        painter.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        for i in range(n):
            cur = points[i]
            prev = points[(i - 1) % n]
            nxt = points[(i + 1) % n]
            path = QPainterPath(_toward(cur, prev, length))
            path.lineTo(cur)
            path.lineTo(_toward(cur, nxt, length))
            painter.drawPath(path)

    def _draw_label(self, painter: QPainter, points, text: str) -> None:
        painter.setFont(_mono(10, True))
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 14
        height = 17

        top_left = min(points, key=lambda p: p.x())
        top = min(points, key=lambda p: p.y())
        x = top_left.x()
        y = top.y() - height - 6
        if y < self._oy + 2:
            y = top.y() + 6

        rect = QRectF(x, y, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(46, 230, 168, 225))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor("#04120c"))
        painter.drawText(rect, Qt.AlignCenter, text)

    def _paint_readout(self, painter: QPainter) -> None:
        if not self._readout:
            return
        painter.setFont(_mono(10))
        painter.setPen(QColor(123, 147, 168, 200))
        rect = QRectF(20, self.height() - 34, self.width() - 40, 18)
        painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, self._readout)

    def _paint_hint(self, painter: QPainter) -> None:
        if not self._hint:
            return
        painter.setFont(_mono(11))
        painter.setPen(QColor(230, 241, 255, 190))
        painter.drawText(QRectF(0, 66, self.width(), 20), Qt.AlignHCenter | Qt.AlignTop, self._hint)

    def _paint_flash(self, painter: QPainter, rect: QRectF) -> None:
        if self._flash <= 0:
            return
        painter.setPen(QPen(QColor(46, 230, 168, int(160 * self._flash)), 5))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(3, 3, -3, -3))

    # ---------------- 交互 ----------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._image is not None and self._tracks:
            pos = event.position()
            for data, track in self._tracks.items():
                if len(track.poly) < 3:
                    continue
                polygon = QPolygonF([self._to_screen(p) for p in track.poly])
                if polygon.containsPoint(pos, Qt.OddEvenFill):
                    self.codeClicked.emit(data)
                    return
        self.emptyClicked.emit()
