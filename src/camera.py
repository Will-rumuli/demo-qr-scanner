import glob
import os
import sys
import time
from typing import List, Optional, Tuple

import cv2
from PySide6.QtCore import QMutex, QThread, Signal

# 名字里含这些词的节点优先尝试（普通 USB 摄像头通常如此）
_PREFERRED_KEYWORDS = ("usb", "camera", "webcam", "uvc")

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
MAX_READ_FAILS = 30      # 连续读帧失败超过此值即认为掉线，重新查找节点


def all_video_nodes() -> List[Tuple[int, str]]:
    """返回所有 (索引, 名称)，名字像摄像头的排在前面。"""
    nodes: List[Tuple[int, str]] = []
    for path in glob.glob("/sys/class/video4linux/video*"):
        try:
            index = int(path.rsplit("video", 1)[-1])
        except ValueError:
            continue
        try:
            with open(os.path.join(path, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        nodes.append((index, name))

    if not nodes:
        # 没有 sysfs（或非 Linux）：退化成探测前几个索引
        return [(i, f"video{i}") for i in range(4)]

    nodes.sort(key=lambda n: n[0])
    preferred = [n for n in nodes if any(k in n[1].lower() for k in _PREFERRED_KEYWORDS)]
    others = [n for n in nodes if n not in preferred]
    return preferred + others


def open_capture(index: int):
    """打开指定节点并完成格式协商，失败返回 None。"""
    if sys.platform.startswith("linux"):
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    else:
        cap = cv2.VideoCapture(index)

    if not cap.isOpened():
        cap.release()
        return None

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    # 只留 1 帧缓冲，降低取流延迟（多码/重连时不会看到旧画面）
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    for _ in range(3):
        ok, frame = cap.read()
        if ok and frame is not None:
            return cap
    cap.release()
    return None


class CameraThread(QThread):
    opened = Signal(int, int, str)          # (generation, index, name)
    failed = Signal(int, str)               # (generation, 致命错误)
    log = Signal(str)                       # 进度日志
    stats = Signal(int, float, int, int)    # (generation, fps, width, height)

    def __init__(self, generation: int, index: Optional[int] = None, parent=None) -> None:
        super().__init__(parent)
        self.generation = generation
        self.index = index
        self._running = True
        self._mutex = QMutex()
        self._frame = None
        self._seq = 0
        self._size = (0, 0)

    # ---------- 对外 ----------

    def latest(self):
        """返回 (最新帧, 序号)。序号递增即代表有新帧，消费端据此跳过旧帧。"""
        self._mutex.lock()
        try:
            return self._frame, self._seq
        finally:
            self._mutex.unlock()

    def request_stop(self) -> None:
        """非阻塞停止：只置标志位，交给 run() 自己退出并释放设备。"""
        self._running = False

    # ---------- 内部 ----------

    def _store(self, frame) -> None:
        self._mutex.lock()
        try:
            self._frame = frame
            self._seq += 1
            h, w = frame.shape[:2]
            self._size = (w, h)
        finally:
            self._mutex.unlock()

    def _find_camera(self):
        for index, name in all_video_nodes():
            if not self._running:
                return None, -1, ""
            self.log.emit(f"Trying /dev/video{index} ({name}) ...")
            cap = open_capture(index)
            if cap is not None:
                return cap, index, name
            self.log.emit(f"  /dev/video{index} not usable")
        return None, -1, ""

    # ---------- 主循环 ----------

    def _open_target(self):
        """按指定索引打开，或自动查找；失败重试几次（USB 设备释放/枚举有延迟）。"""
        for attempt in range(3):
            if not self._running:
                return None, -1, ""
            if self.index is not None:
                self.log.emit(f"Opening /dev/video{self.index} ...")
                cap = open_capture(self.index)
                index, name = self.index, f"video{self.index}"
            else:
                cap, index, name = self._find_camera()
            if cap is not None:
                return cap, index, name
            if attempt < 2:
                self.log.emit("Camera not ready, retrying ...")
                self.msleep(800)
        return None, -1, ""

    def run(self) -> None:
        cap, index, name = self._open_target()
        if cap is None:
            if self._running:
                self.failed.emit(self.generation, "No working camera found")
            return

        self.opened.emit(self.generation, index, name)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        fails = 0
        frames = 0
        started = time.perf_counter()

        while self._running:
            ok, frame = cap.read()
            if not ok:
                fails += 1
                if fails >= MAX_READ_FAILS:
                    cap.release()
                    self.log.emit("Stream lost, searching for camera again ...")
                    cap, index, name = self._find_camera()
                    if cap is None:
                        self.failed.emit(self.generation, "Camera disconnected")
                        return
                    self.log.emit(f"Reconnected: /dev/video{index} ({name})")
                    self.opened.emit(self.generation, index, name)
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    fails = 0
                    # 丢弃重连后缓冲里的旧帧
                    for _ in range(3):
                        cap.grab()
                self.msleep(10)
                continue

            fails = 0
            self._store(frame)

            frames += 1
            now = time.perf_counter()
            if now - started >= 1.0:
                self.stats.emit(self.generation, frames / (now - started), width, height)
                frames = 0
                started = now

        cap.release()
