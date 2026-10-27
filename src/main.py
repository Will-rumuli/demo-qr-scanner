import os
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow

from camera import CameraThread
from detector import CodeDetector, DetectionWorker
from hud import HudContainer
from payload import ScanRecord, parse

# 同一个码连续出现多少帧才收录。3 帧 ≈ 100ms：既有可见的锁定动画，又不拖手感
STABLE_FRAMES = max(1, int(os.environ.get("SCAN_STABLE_FRAMES", "3")))
# 同一个码连续丢失多少批次后才允许重新收录（抗检测抖动）
MISS_RESET = 2


class Beep:
    """识别成功提示音。用 numpy 现场生成一个短促的双频提示音，不依赖外部音频文件。"""

    def __init__(self) -> None:
        self._effect = None
        self._ok = False
        try:
            import tempfile
            import wave

            import numpy as np
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

            rate = 44100
            count = int(rate * 0.075)
            t = np.arange(count) / rate
            envelope = np.exp(-np.linspace(0.0, 5.5, count))
            tone = np.sin(2 * np.pi * 1500 * t) + 0.35 * np.sin(2 * np.pi * 3000 * t)
            pcm = np.clip(tone * envelope * 0.22, -1.0, 1.0)

            path = os.path.join(tempfile.mkdtemp(prefix="qr_beep_"), "beep.wav")
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes((pcm * 32767).astype("<i2").tobytes())

            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(path))
            effect.setVolume(0.7)
            self._effect = effect
            self._ok = True
        except Exception:
            # 没有 QtMultimedia 或音频设备时退化为系统提示音
            self._ok = False

    def play(self) -> None:
        if self._ok and self._effect is not None:
            try:
                self._effect.play()
                return
            except Exception:
                pass
        try:
            QApplication.beep()
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("QR / Barcode Scanner")
        self.resize(1280, 800)

        self.detector = CodeDetector()
        self.hud = HudContainer()
        self.setCentralWidget(self.hud)

        self.generation = 0
        self.camera: CameraThread | None = None
        self.worker: DetectionWorker | None = None

        self.records: list[ScanRecord] = []
        # 已请求停止、但线程可能还在收尾的对象。必须一直持有引用，
        # 否则 Python 回收 QThread 时线程仍在运行，Qt 会直接 abort。
        self._retiring: list = []
        self._last_codes = []
        self._streak: dict[str, int] = {}
        self._miss: dict[str, int] = {}
        self._active: set[str] = set()

        self._last_seq = -1
        self._fps = 0.0
        self._latency = 0.0
        self._size = (0, 0)

        self._beep = Beep()
        self._wire()
        self._start_render_timer()

        self.hud.top.set_status("READY", "idle", "standby")
        self.hud.diag.set_stat("backend", self.detector.backend)
        self.hud.view.set_idle_text("CAMERA IDLE")
        self._log(f"Detection backend: {self.detector.backend}")
        self._log("Ready. Press SPACE or click 'Start Camera'.")

    # ---------------- 组装 ----------------

    def _wire(self) -> None:
        self.hud.startClicked.connect(self._start)
        self.hud.stopClicked.connect(lambda: self._stop(user=True))
        self.hud.cardClicked.connect(self.hud.detail.show_record)
        self.hud.view.codeClicked.connect(self._on_code_clicked)

        for key, slot in (("Space", self._toggle), ("D", self._toggle_diagnostics),
                          ("Escape", self._escape)):
            QShortcut(QKeySequence(key), self).activated.connect(slot)

    def _start_render_timer(self) -> None:
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(33)
        self._render_timer.timeout.connect(self._render_frame)
        self._render_timer.start()

    # ---------------- 摄像头生命周期 ----------------

    @staticmethod
    def _argv_index():
        if len(sys.argv) > 1:
            try:
                return int(sys.argv[1])
            except ValueError:
                return None
        return None

    def _start(self) -> None:
        if self.camera is not None:
            return

        self.generation += 1
        generation = self.generation

        camera = CameraThread(generation, self._argv_index())
        camera.opened.connect(self._on_opened)
        camera.failed.connect(self._on_failed)
        camera.log.connect(self._log)
        camera.stats.connect(self._on_stats)
        self.camera = camera
        camera.start()

        worker = DetectionWorker(generation, camera, self.detector)
        worker.detections.connect(self._on_detections)
        self.worker = worker
        worker.start()

        self._streak.clear()
        self._miss.clear()
        self._active.clear()
        self._last_codes = []
        self._last_seq = -1
        self._latency = 0.0
        self._fps = 0.0

        self.hud.view.set_live(True)
        self.hud.view.clear_codes()
        self.hud.view.clear_frame()
        self.hud.view.set_idle_text("SEARCHING FOR CAMERA")
        self.hud.view.set_hint("Aim a QR code or barcode at the camera")
        self.hud.view.set_readout("")
        self.hud.ctrl.set_running(True)
        self.hud.top.set_status("CONNECTING", "warn", "looking for a camera")
        self._log("Scan session started.")

    def _stop(self, user: bool = False, wait: bool = False) -> None:
        worker, self.worker = self.worker, None
        camera, self.camera = self.camera, None

        if worker is not None:
            worker.request_stop()
        if camera is not None:
            camera.request_stop()

        if wait:
            # 仅在退出时使用：等线程真正收尾，避免 QThread 在运行中被销毁
            for thread in (worker, camera):
                if thread is not None:
                    thread.wait(1500)

        for thread in (worker, camera):
            if thread is None:
                continue
            # 线程自己跑完之前一直保留引用，跑完后再交给 deleteLater
            self._retiring.append(thread)
            thread.finished.connect(lambda t=thread: self._forget(t))
            thread.finished.connect(thread.deleteLater)

        self._streak.clear()
        self._miss.clear()
        self._active.clear()
        self._last_codes = []

        self.hud.view.set_live(False)
        self.hud.view.clear_codes()
        self.hud.view.clear_frame()
        self.hud.view.set_hint("")
        self.hud.view.set_readout("")
        self.hud.view.set_idle_text("CAMERA STOPPED" if user else "CAMERA IDLE")
        self.hud.ctrl.set_running(False)

        if user:
            self.hud.top.set_status("READY", "idle", "standby")
            self._log("Camera stopped.")

    def _on_opened(self, generation: int, index: int, name: str) -> None:
        if generation != self.generation:
            return
        self._log(f"Camera opened: /dev/video{index} ({name})")
        self.hud.top.set_status("SCANNING", "live", f"/dev/video{index} · {name[:26]}")
        self.hud.view.set_idle_text("SCANNING")

    def _on_failed(self, generation: int, message: str) -> None:
        if generation != self.generation:
            return
        self._log(message)
        self._stop(user=False)
        self.hud.top.set_status("ERROR", "err", message)
        self.hud.view.set_idle_text(message.upper())

    # ---------------- 数据流 ----------------

    def _render_frame(self) -> None:
        camera = self.camera
        if camera is None:
            return
        frame, seq = camera.latest()
        if frame is None or seq == self._last_seq:
            return
        self._last_seq = seq
        self.hud.view.set_frame(frame)

        if self._size == (0, 0):
            h, w = frame.shape[:2]
            self._size = (w, h)
            self.hud.diag.set_stat("resolution", f"{w}×{h}")

    def _on_detections(self, generation: int, codes, latency: float) -> None:
        if generation != self.generation or self.camera is None:
            return

        self._latency = latency
        self._last_codes = codes

        present = set()
        for code in codes:
            present.add(code.data)
            self._streak[code.data] = self._streak.get(code.data, 0) + 1
            self._miss[code.data] = 0

        for data in list(self._streak):
            if data in present:
                continue
            self._miss[data] = self._miss.get(data, 0) + 1
            if self._miss[data] >= MISS_RESET:
                self._streak.pop(data, None)
                self._miss.pop(data, None)
                self._active.discard(data)

        progress = {d: min(1.0, s / STABLE_FRAMES) for d, s in self._streak.items()}
        self.hud.view.set_codes(codes, progress)

        for code in codes:
            if code.data in self._active:
                continue
            if self._streak.get(code.data, 0) >= STABLE_FRAMES:
                self._active.add(code.data)
                self._accept(code, latency)

        self.hud.diag.set_stat("latency", f"{latency:.1f} ms")
        self._refresh_readout()

    def _on_stats(self, generation: int, fps: float, width: int, height: int) -> None:
        if generation != self.generation:
            return
        self._fps = fps
        self._size = (width, height)
        self.hud.diag.set_stat("fps", f"{fps:.1f}")
        self.hud.diag.set_stat("resolution", f"{width}×{height}")
        self._refresh_readout()

    def _refresh_readout(self) -> None:
        width, height = self._size
        parts = [f"FPS {self._fps:.1f}",
                 f"DECODE {self._latency:.1f}ms",
                 self.detector.backend.upper()]
        if width:
            parts.append(f"{width}×{height}")
        self.hud.view.set_readout("   ·   ".join(parts))

    # ---------------- 收录 ----------------

    def _accept(self, code, latency: float) -> None:
        payload = parse(code.data, code.symbology)
        record = ScanRecord(data=code.data, symbology=code.symbology,
                            latency=latency, ts=time.time(), payload=payload)
        self.records.append(record)

        self.hud.rail.add_record(record, len(self.records))
        self.hud.view.trigger_accept(code.data)
        self._beep.play()
        self._update_counts()
        self._log(f"ACCEPT [{code.symbology}] {payload.title}")

    def _on_code_clicked(self, data: str) -> None:
        if data in self._active:
            return
        for code in self._last_codes:
            if code.data == data:
                self._active.add(data)
                self._accept(code, self._latency)
                return

    def _update_counts(self) -> None:
        unique = len({record.data for record in self.records})
        avg = sum(record.latency for record in self.records) / len(self.records) if self.records else 0.0
        self.hud.top.set_counts(len(self.records), unique, avg)

    # ---------------- 交互 ----------------

    def _toggle(self) -> None:
        if self.hud.detail.isVisible():
            return
        if self.camera is None:
            self._start()
        else:
            self._stop(user=True)

    def _toggle_diagnostics(self) -> None:
        visible = not self.hud.diag.isVisible()
        self.hud.set_diag_visible(visible)

    def _escape(self) -> None:
        if self.hud.detail.isVisible():
            self.hud.detail.hide()
        elif self.camera is not None:
            self._stop(user=True)

    # ---------------- 杂项 ----------------

    def _forget(self, thread) -> None:
        try:
            self._retiring.remove(thread)
        except ValueError:
            pass

    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.hud.diag.append_log(f"[{stamp}] {message}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop(user=False, wait=True)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("QR Scanner HUD")
    window = MainWindow()
    window.show()
    # 启动即开始扫码，省掉一次点击；Esc / Stop 仍然可以随时停下
    QTimer.singleShot(0, window._start)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
