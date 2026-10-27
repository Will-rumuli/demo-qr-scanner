import time
import zlib

from PySide6.QtCore import (
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from payload import ScanRecord, is_2d
from preview import ScannerView

HUD_QSS = """
QWidget#hudRoot { background: #05080d; }

QLabel#statusPill {
    padding: 5px 14px; border-radius: 13px;
    background: rgba(123,147,168,22); border: 1px solid rgba(123,147,168,90);
    color: #9fb4c7; font-family: monospace; font-size: 11px; font-weight: 700;
}
QLabel#statusPill[tone="live"] {
    background: rgba(46,230,168,26); border: 1px solid rgba(46,230,168,140); color: #6ff0c6;
}
QLabel#statusPill[tone="warn"] {
    background: rgba(251,191,36,24); border: 1px solid rgba(251,191,36,130); color: #fbd77a;
}
QLabel#statusPill[tone="err"] {
    background: rgba(248,113,113,24); border: 1px solid rgba(248,113,113,130); color: #fca5a5;
}
QLabel#statusSub { color: #5d7488; font-family: monospace; font-size: 10px; }
QLabel#statValue { color: #d9fff1; font-family: monospace; font-size: 17px; font-weight: 700; }
QLabel#statCap { color: #55697c; font-family: monospace; font-size: 9px; }

QWidget#railPanel {
    background: rgba(8,16,25,210); border: 1px solid rgba(70,110,140,80); border-radius: 14px;
}
QLabel#railTitle { color: #7b93a8; font-family: monospace; font-size: 10px; font-weight: 700; }
QLabel#railCount { color: #2ee6a8; font-family: monospace; font-size: 10px; font-weight: 700; }
QLabel#railEmpty { color: #4f6376; font-family: monospace; font-size: 11px; }
QScrollArea#railScroll { background: transparent; border: none; }
QWidget#railInner { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(120,160,190,90); border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QPushButton#primaryBtn {
    background: rgba(46,230,168,235); color: #04120c; border: none; border-radius: 10px;
    padding: 12px 22px; font-family: monospace; font-size: 12px; font-weight: 700;
}
QPushButton#primaryBtn:hover { background: #4ff2ba; }
QPushButton#ghostBtn {
    background: rgba(255,255,255,14); color: #cfe0f0;
    border: 1px solid rgba(120,160,190,80); border-radius: 10px;
    padding: 12px 18px; font-family: monospace; font-size: 12px; font-weight: 600;
}
QPushButton#ghostBtn:hover { background: rgba(255,255,255,26); }
QPushButton#ghostBtn:disabled {
    color: #44576a; border-color: rgba(120,160,190,40); background: rgba(255,255,255,6);
}
QPushButton#ghostBtn:checked {
    background: rgba(46,230,168,40); border-color: rgba(46,230,168,140); color: #7ff0c8;
}

QWidget#diagPanel {
    background: rgba(6,12,20,235); border: 1px solid rgba(70,110,140,90); border-radius: 12px;
}
QLabel#diagTitle { color: #7b93a8; font-family: monospace; font-size: 10px; font-weight: 700; }
QLabel#diagKey { color: #55697c; font-family: monospace; font-size: 10px; }
QLabel#diagVal { color: #cfe0f0; font-family: monospace; font-size: 10px; font-weight: 600; }
QPlainTextEdit#diagLog {
    background: rgba(0,0,0,100); border: 1px solid rgba(70,110,140,60); border-radius: 8px;
    color: #7fd6a8; font-family: monospace; font-size: 10px;
}

QWidget#detailPanel {
    background: rgba(8,16,26,245); border: 1px solid rgba(70,110,140,120); border-radius: 16px;
}
QLabel#detailKind { color: #2ee6a8; font-family: monospace; font-size: 10px; font-weight: 700; }
QLabel#detailTitle { color: #eaf6ff; font-size: 22px; font-weight: 700; }
QLabel#detailSub { color: #7b93a8; font-size: 12px; }
QLabel#detailKey { color: #55697c; font-family: monospace; font-size: 11px; }
QLabel#detailVal { color: #dbe9f5; font-family: monospace; font-size: 11px; }
QLabel#detailRaw {
    background: rgba(0,0,0,110); border: 1px solid rgba(70,110,140,70); border-radius: 8px;
    color: #8fd8ff; font-family: monospace; font-size: 11px; padding: 8px;
}
"""

CARD_HEIGHT = 86
RAIL_WIDTH = 336

_KIND_COLORS = {
    "url": QColor("#38bdf8"),
    "wifi": QColor("#2ee6a8"),
    "contact": QColor("#c084fc"),
    "gtin": QColor("#fbbf24"),
    "phone": QColor("#34d399"),
    "text": QColor("#94a8bb"),
}


# ------------------------------------------------------------------ 顶部状态栏

class _Stat(QWidget):
    def __init__(self, caption: str, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.value = QLabel("0")
        self.value.setObjectName("statValue")
        cap = QLabel(caption)
        cap.setObjectName("statCap")
        layout.addWidget(self.value, 0, Qt.AlignRight)
        layout.addWidget(cap, 0, Qt.AlignRight)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class TopBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.pill = QLabel("READY")
        self.pill.setObjectName("statusPill")
        self.pill.setProperty("tone", "idle")

        self.sub = QLabel("standby")
        self.sub.setObjectName("statusSub")

        self.stat_scans = _Stat("SCANS")
        self.stat_unique = _Stat("UNIQUE")
        self.stat_avg = _Stat("AVG DECODE")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 0)
        layout.setSpacing(14)
        layout.addWidget(self.pill, 0, Qt.AlignVCenter)
        layout.addWidget(self.sub, 0, Qt.AlignVCenter)
        layout.addStretch(1)
        layout.addWidget(self.stat_scans)
        layout.addWidget(self.stat_unique)
        layout.addWidget(self.stat_avg)

    def set_status(self, text: str, tone: str = "idle", sub: str = "") -> None:
        self.pill.setText(text)
        if self.pill.property("tone") != tone:
            self.pill.setProperty("tone", tone)
            self.pill.style().unpolish(self.pill)
            self.pill.style().polish(self.pill)
        if sub:
            self.sub.setText(sub)

    def set_counts(self, scans: int, unique: int, avg_ms: float) -> None:
        self.stat_scans.set_value(str(scans))
        self.stat_unique.set_value(str(unique))
        self.stat_avg.set_value("--" if scans == 0 else f"{avg_ms:.0f}ms")


# ------------------------------------------------------------------ 结果卡片

class ResultCard(QWidget):
    clicked = Signal(object)

    def __init__(self, record: ScanRecord, parent=None) -> None:
        super().__init__(parent)
        self.record = record
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        # 固定高度：滚动区不该压缩卡片，否则卡片一多就被挤成 0 高
        self.setFixedHeight(CARD_HEIGHT)
        self.setToolTip(record.data)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(RAIL_WIDTH - 30, CARD_HEIGHT)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.record)

    def paintEvent(self, event) -> None:  # noqa: N802
        # 入场动画过程中高度很小，先不画任何内容（也避免负高度矩形）
        if self.height() < CARD_HEIGHT - 12:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0, 3, 0, -3)

        painter.setPen(QPen(QColor(120, 160, 190, 60 if self._hover else 36), 1))
        painter.setBrush(QColor(255, 255, 255, 22 if self._hover else 10))
        painter.drawRoundedRect(rect, 10, 10)

        record = self.record
        color = _KIND_COLORS.get(record.payload.kind, _KIND_COLORS["text"])

        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(rect.left() + 7, rect.top() + 16, 3, rect.height() - 32), 2, 2)

        glyph = QRectF(rect.left() + 18, rect.center().y() - 18, 36, 36)
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 26))
        painter.drawRoundedRect(glyph, 8, 8)
        self._paint_glyph(painter, glyph.adjusted(7, 7, -7, -7), color, record)

        text_left = glyph.right() + 12
        text_width = rect.right() - text_left - 12

        title_font = QFont(self.font())
        title_font.setPixelSize(13)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#eaf6ff"))
        title = QFontMetrics(title_font).elidedText(record.payload.title, Qt.ElideRight, int(text_width))
        painter.drawText(QRectF(text_left, rect.top() + 14, text_width, 18), Qt.AlignLeft | Qt.AlignVCenter, title)

        sub_font = QFont(self.font())
        sub_font.setPixelSize(10)
        painter.setFont(sub_font)
        painter.setPen(QColor("#7b93a8"))
        metrics = QFontMetrics(sub_font)
        subtitle = metrics.elidedText(record.payload.subtitle, Qt.ElideRight, int(text_width))
        painter.drawText(QRectF(text_left, rect.top() + 33, text_width, 14), Qt.AlignLeft | Qt.AlignVCenter, subtitle)

        # 底部一行：码制 chip + 耗时 + 时间
        chip_font = QFont(self.font())
        chip_font.setPixelSize(9)
        chip_font.setBold(True)
        painter.setFont(chip_font)
        chip_metrics = QFontMetrics(chip_font)
        chip_text = record.symbology
        chip_width = chip_metrics.horizontalAdvance(chip_text) + 12
        chip_rect = QRectF(text_left, rect.bottom() - 24, chip_width, 15)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 34))
        painter.drawRoundedRect(chip_rect, 7, 7)
        painter.setPen(color)
        painter.drawText(chip_rect, Qt.AlignCenter, chip_text)

        meta_font = QFont(self.font())
        meta_font.setPixelSize(9)
        meta_font.setFamily("monospace")
        painter.setFont(meta_font)
        painter.setPen(QColor("#55697c"))
        stamp = time.strftime("%H:%M:%S", time.localtime(record.ts))
        meta = f"{record.latency:.0f}ms   {stamp}"
        painter.drawText(QRectF(chip_rect.right() + 8, rect.bottom() - 24,
                                rect.right() - chip_rect.right() - 20, 15),
                         Qt.AlignRight | Qt.AlignVCenter, meta)

    def _paint_glyph(self, painter: QPainter, rect: QRectF, color: QColor, record: ScanRecord) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        if is_2d(record.symbology):
            # 5x5 迷你二维码图案（由内容哈希决定，固定不变）
            seed = zlib.crc32(record.data.encode("utf-8", "replace"))
            cell = rect.width() / 5.0
            for row in range(5):
                for col in range(5):
                    on = bool((seed >> ((row * 5 + col) % 31)) & 1)
                    if row < 2 and col < 2:
                        on = True
                    if on:
                        painter.drawRect(QRectF(rect.left() + col * cell,
                                                rect.top() + row * cell, cell - 0.6, cell - 0.6))
        else:
            widths = (0.16, 0.08, 0.22, 0.1, 0.18, 0.07, 0.19)
            x = rect.left()
            for w in widths:
                bar = w * rect.width()
                if x + bar > rect.right():
                    break
                painter.drawRect(QRectF(x, rect.top(), max(1.0, bar), rect.height()))
                x += bar + rect.width() * 0.045


# ------------------------------------------------------------------ 结果栏

class ResultRail(QWidget):
    cardClicked = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("railPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.title = QLabel("SESSION")
        self.title.setObjectName("railTitle")
        self.count = QLabel("0 scans")
        self.count.setObjectName("railCount")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.count)

        self._inner = QWidget()
        self._inner.setObjectName("railInner")
        self._list = QVBoxLayout(self._inner)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)

        self._empty = QLabel("No scans yet.\nPoint the camera at a code.")
        self._empty.setObjectName("railEmpty")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._list.addWidget(self._empty)
        self._list.addStretch(1)
        self._cards: list[ResultCard] = []

        scroll = QScrollArea()
        scroll.setObjectName("railScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(self._inner)
        self._scroll = scroll

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 10, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)

    def add_record(self, record: ScanRecord, total: int) -> None:
        self._empty.hide()
        card = ResultCard(record)
        card.clicked.connect(self.cardClicked.emit)
        self._list.insertWidget(0, card)
        self._cards.append(card)

        # 只做淡入，不动高度：卡片必须保持固定高，否则卡片一多，
        # 滚动区会把每张卡压扁到几像素，整栏看起来像是被清空了
        effect = QGraphicsOpacityEffect(card)
        effect.setOpacity(0.0)
        card.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", card)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setDuration(200)
        fade.finished.connect(lambda: card.setGraphicsEffect(None))
        fade.start()

        self.count.setText(f"{total} scan{'s' if total != 1 else ''}")
        # 新卡插在最上面，把视图拉回顶部，保证一眼能看到最新结果
        self._scroll.verticalScrollBar().setValue(0)

    def reset(self) -> None:
        for card in self._cards:
            self._list.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._empty.show()
        self.count.setText("0 scans")


# ------------------------------------------------------------------ 底部控制条

class ControlBar(QWidget):
    startClicked = Signal()
    stopClicked = Signal()
    diagClicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.start_btn = QPushButton("Start Camera")
        self.start_btn.setObjectName("primaryBtn")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("ghostBtn")
        self.diag_btn = QPushButton("Diagnostics")
        self.diag_btn.setObjectName("ghostBtn")
        self.diag_btn.setCheckable(True)
        self.stop_btn.setEnabled(False)

        for button in (self.start_btn, self.stop_btn, self.diag_btn):
            button.setFocusPolicy(Qt.NoFocus)

        self.start_btn.clicked.connect(self.startClicked.emit)
        self.stop_btn.clicked.connect(self.stopClicked.emit)
        self.diag_btn.clicked.connect(self.diagClicked.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.start_btn)
        layout.addWidget(self.stop_btn)
        layout.addWidget(self.diag_btn)

    def set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)


# ------------------------------------------------------------------ 诊断抽屉

class DiagnosticsPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("diagPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)

        title = QLabel("DIAGNOSTICS")
        title.setObjectName("diagTitle")

        self._values: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        for i, (key, label) in enumerate((
            ("backend", "BACKEND"),
            ("resolution", "RESOLUTION"),
            ("fps", "FPS"),
            ("latency", "DECODE"),
        )):
            key_label = QLabel(label)
            key_label.setObjectName("diagKey")
            value = QLabel("--")
            value.setObjectName("diagVal")
            grid.addWidget(key_label, i // 2, (i % 2) * 2)
            grid.addWidget(value, i // 2, (i % 2) * 2 + 1)
            self._values[key] = value

        self.log = QPlainTextEdit()
        self.log.setObjectName("diagLog")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addLayout(grid)
        layout.addWidget(self.log, 1)

    def set_stat(self, key: str, value: str) -> None:
        label = self._values.get(key)
        if label is not None:
            label.setText(value)

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)


# ------------------------------------------------------------------ 详情浮层

class DetailOverlay(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("detailOverlay")
        self._record: ScanRecord | None = None
        self.setFocusPolicy(Qt.StrongFocus)

        self.panel = QWidget(self)
        self.panel.setObjectName("detailPanel")
        self.panel.setAttribute(Qt.WA_StyledBackground, True)

        self.kind = QLabel("")
        self.kind.setObjectName("detailKind")
        self.title = QLabel("")
        self.title.setObjectName("detailTitle")
        self.title.setWordWrap(True)
        self.sub = QLabel("")
        self.sub.setObjectName("detailSub")
        self.sub.setWordWrap(True)

        self._fields_host = QWidget(self.panel)
        self._fields = QGridLayout(self._fields_host)
        self._fields.setContentsMargins(0, 0, 0, 0)
        self._fields.setHorizontalSpacing(14)
        self._fields.setVerticalSpacing(4)

        self.raw = QLabel("")
        self.raw.setObjectName("detailRaw")
        self.raw.setWordWrap(True)
        self.raw.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.raw.setMaximumHeight(84)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("ghostBtn")
        self.copy_btn.clicked.connect(self._copy)
        self.close_btn = QPushButton("Close")
        self.close_btn.setObjectName("primaryBtn")
        self.close_btn.clicked.connect(self.hide)
        for button in (self.copy_btn, self.close_btn):
            button.setFocusPolicy(Qt.NoFocus)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(6)
        layout.addWidget(self.kind)
        layout.addWidget(self.title)
        layout.addWidget(self.sub)
        layout.addSpacing(8)
        layout.addWidget(self._fields_host)
        layout.addSpacing(6)
        layout.addWidget(self.raw)
        layout.addSpacing(10)
        layout.addLayout(buttons)

        self.hide()

    # ---------- 展示 ----------

    def show_record(self, record: ScanRecord) -> None:
        self._record = record
        self.kind.setText(f"{record.symbology.upper()}   ·   {record.latency:.1f} ms decode")
        self.title.setText(record.payload.title)
        self.sub.setText(record.payload.subtitle)

        while self._fields.count():
            item = self._fields.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for row, (key, value) in enumerate(record.payload.fields[:6]):
            key_label = QLabel(key)
            key_label.setObjectName("detailKey")
            value_label = QLabel(str(value))
            value_label.setObjectName("detailVal")
            value_label.setWordWrap(True)
            self._fields.addWidget(key_label, row, 0, Qt.AlignTop)
            self._fields.addWidget(value_label, row, 1)

        raw = record.data if len(record.data) <= 400 else record.data[:400] + "..."
        self.raw.setText(raw)

        self.show()
        self.raise_()
        self.setFocus()
        self._layout_panel()

    def _copy(self) -> None:
        if self._record is not None:
            QApplication.clipboard().setText(self._record.data)
            self.copy_btn.setText("Copied")
            self.copy_btn.setEnabled(False)
            QTimer.singleShot(900, self._restore_copy)

    def _restore_copy(self) -> None:
        self.copy_btn.setText("Copy payload")
        self.copy_btn.setEnabled(True)

    # ---------- 事件 ----------

    def _layout_panel(self) -> None:
        width = min(600, max(360, self.width() - 80))
        self.panel.resize(width, min(self.height() - 80, self.panel.sizeHint().height()))
        self.panel.move((self.width() - self.panel.width()) // 2,
                        (self.height() - self.panel.height()) // 2)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._layout_panel()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(3, 7, 12, 200))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.hide()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)


# ------------------------------------------------------------------ 总容器

class HudContainer(QWidget):
    startClicked = Signal()
    stopClicked = Signal()
    diagToggled = Signal()
    cardClicked = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("hudRoot")
        self.setStyleSheet(HUD_QSS)

        self.view = ScannerView(self)
        self.top = TopBar(self)
        self.rail = ResultRail(self)
        self.ctrl = ControlBar(self)
        self.diag = DiagnosticsPanel(self)
        self.detail = DetailOverlay(self)
        self.diag.hide()

        self.ctrl.startClicked.connect(self.startClicked.emit)
        self.ctrl.stopClicked.connect(self.stopClicked.emit)
        self.ctrl.diagClicked.connect(self._on_diag_clicked)
        self.rail.cardClicked.connect(self.cardClicked.emit)

    def _on_diag_clicked(self) -> None:
        visible = not self.diag.isVisible()
        self.set_diag_visible(visible)
        self.ctrl.diag_btn.setChecked(visible)
        self.diagToggled.emit()

    def set_diag_visible(self, visible: bool) -> None:
        self.diag.setVisible(visible)
        self.ctrl.diag_btn.setChecked(visible)
        self._layout_children()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._layout_children()

    def _layout_children(self) -> None:
        width, height = self.width(), self.height()
        margin = 20

        self.view.setGeometry(0, 0, width, height)
        self.detail.setGeometry(0, 0, width, height)

        self.top.setGeometry(margin, 12, max(0, width - margin * 2), 46)

        ctrl_height = self.ctrl.sizeHint().height() if self.ctrl.sizeHint().height() > 0 else 46
        ctrl_width = min(460, max(0, width - margin * 2))
        self.ctrl.setGeometry(margin, height - margin - ctrl_height, ctrl_width, ctrl_height)

        rail_top = 76
        rail_bottom = height - margin - ctrl_height - 10
        self.rail.setGeometry(width - margin - RAIL_WIDTH, rail_top,
                              RAIL_WIDTH, max(160, rail_bottom - rail_top))

        if self.diag.isVisible():
            diag_height = 300
            self.diag.setGeometry(margin, height - margin - ctrl_height - 10 - diag_height,
                                  min(470, max(0, width - margin * 2 - RAIL_WIDTH - 20)), diag_height)
