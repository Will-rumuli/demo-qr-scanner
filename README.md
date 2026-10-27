# demo-qr-scanner

[中文](README.zh-CN.md)

A PySide6 + OpenCV QR / barcode scanner demo with an industrial HUD overlay: live multi-code tracking, smart payload parsing, and a session result feed.

The whole window is the camera view. Everything else — status bar, lock-on frames, result cards, detail panel — floats on top of it.

![demo_qr_scan_3](resource/demo_qr_scan_3.png)

## Features

- **Industrial HUD**: scanline, center reticle, corner brackets, per-code outline that lights up as the code becomes stable, white flash on accept.
- **Multi-code, session based**: every code in view is tracked at once, and the camera keeps running. Results stack up in a session rail instead of the app stopping after the first scan.
- **Stable-frame confirmation**: a code must be decoded in 3 consecutive frames before it is accepted, which removes most flicker and false hits. Re-scanning the same content is allowed again after it leaves the frame for 2 frames.
- **Smart payload parsing**: raw strings are turned into readable cards (URL, Wi-Fi, contact, product code, phone, mail, plain text) with GTIN check-digit validation and GS1 country lookup.
- **Detail overlay**: click a card or a lock-on frame to see parsed fields plus the raw payload, with a Copy button.
- **Diagnostics drawer**: active backend, resolution, FPS, decode latency and a live log.
- **Audio feedback**: a short 1500 Hz + 3000 Hz blip generated at runtime, with a graceful fallback to the system beep.

## How it works

```
CameraThread  ──latest frame (single slot, overwritten)──▶  DetectionWorker
(OpenCV VideoCapture)                                      (downscale + decode)
      │                                                            │
      └──────────────▶ GUI render timer (33 ms) ◀──────────────────┘
                              │
                        ScannerView / HUD
```

- **Capture and decode run in separate threads.** The camera thread only grabs frames and keeps the newest one in a mutex-protected single slot, so a slow decode never backs up the capture queue.
- **Decode always works on the newest frame** and drops everything older, so latency stays flat instead of growing.
- **Decoding is done on a 960 px wide copy** and the returned polygon is scaled back to the original frame coordinates. 720p decode cost is roughly halved.
- **Backend priority**: `zxing-cpp` → `pyzbar` → OpenCV built-in detector. Any missing backend degrades gracefully; the active one is shown in the diagnostics drawer.
- **Detection is throttled** to about 20 fps (45 ms minimum interval), which keeps a 4-core ARM board from burning a full core while feeling instant.

## Requirements

- Python 3.9+
- A UVC camera (built-in or USB); 1280×720 MJPG is requested by default
- On Debian / Ubuntu: `python3-venv` (virtual environment tooling) and `libzbar` (the native library `pyzbar` binds to, pip cannot provide it)

```bash
sudo apt update
sudo apt install -y python3-venv libzbar0t64   # this demo runs on Debian 13; Debian 12 / Ubuntu: libzbar0
```

Create and populate a virtual environment:

```bash
cd qr_scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# start the demo
python3 src/main.py
```

Click Start Camera to start scanning:

![demo_qr_scan_1](resource/demo_qr_scan_1.png)
![demo_qr_scan_2](resource/demo_qr_scan_2.png)

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `SCAN_STABLE_FRAMES` | `3` | Consecutive successful frames required before a code is accepted. Lower = faster lock, higher = fewer false positives. |

## Parsed payload types

| Kind | Example input | Card shows |
| --- | --- | --- |
| `url` | `https://www.quectel.com/product/rm520n` | host, path, scheme |
| `wifi` | `WIFI:T:WPA;S:Quectel-Demo;P:12345678;;` | SSID, security, password |
| `contact` | `BEGIN:VCARD...` / `MECARD:N:Zhang;TEL:...;;` | name, phone, email, org |
| `gtin` | `6901234567892` | GS1 country, check digit status, ISBN detection |
| `phone` | `tel:+8613800138000` | number |
| `text` | anything else | raw text, length |

## Interface guide

The UI has five parts: top bar, camera view, session rail, bottom controls, and overlays that appear on demand.

### Top bar

| Element | Meaning |
| --- | --- |
| Status pill | `READY` idle / `CONNECTING` opening the camera (amber) / `SCANNING` scanning (green) / `ERROR` failure (red) |
| Text next to the pill | Active camera node and model, e.g. `/dev/video60 · UGREEN Camera 2K` |
| `SCANS` | Total accepted scans in this session; rescanning the same code keeps counting |
| `UNIQUE` | Distinct payloads after de-duplication; the same code scanned 5 times still counts as 1 |
| `AVG DECODE` | Average decode latency of all accepted records |

### Camera view

| Element | Meaning |
| --- | --- |
| Scanline / center reticle | Running indicator, shown only while scanning |
| Corner brackets | Position of a detected code; the outline lights up along the contour as the code becomes stable, and completes when it is accepted |
| Symbology chip | Detected code type, e.g. `QR Code`, `EAN-13` |
| White flash | One-shot feedback on a successful accept |
| Top hint | `Aim a QR code or barcode at the camera` |
| Bottom-right readout | `FPS` capture rate, `DECODE` last decode latency, active backend (e.g. `ZXING`), frame resolution |
| Center text | `CAMERA IDLE` before start, `CAMERA STOPPED` after stop |

### Session rail

| Element | Meaning |
| --- | --- |
| `SESSION` | Rail title; `n scans` on the right mirrors `SCANS` in the top bar |
| Color bar | Payload kind: blue = URL, green = Wi-Fi, purple = contact, amber = product code, gray = text |
| Title / subtitle | Parsed summary, e.g. host + path |
| Symbology chip | Code type of this record |
| `56ms` / timestamp | Decode latency of this record and when it was accepted (HH:MM:SS) |
| Clicking a card | Opens the detail overlay |
| Empty state | `No scans yet. Point the camera at a code.` |

### Bottom controls

| Button | Action |
| --- | --- |
| `Start Camera` | Opens the camera and starts scanning; disabled while running |
| `Stop` | Stops scanning and releases the camera; disabled while idle |
| `Diagnostics` | Toggles the diagnostics drawer, same as the `D` key |

### Detail overlay

Opened by clicking a result card or a lock-on frame in the camera view.

| Element | Meaning |
| --- | --- |
| Small header | Symbology + latency, e.g. `QR CODE · 56.5 ms decode` |
| Title / subtitle | Parsed headline and supporting line |
| Field grid | Structured fields per payload kind (host, path, scheme, SSID, name, country, check digit, ...) |
| `RAW PAYLOAD` | Untouched original string |
| `Copy payload` | Copies the raw string; the label briefly changes to `Copied` |
| `Close` | Closes the overlay; clicking outside it or pressing `Esc` also works |

### Diagnostics drawer

| Element | Meaning |
| --- | --- |
| `BACKEND` | Active decode backend: `zxing` / `zbar` / `opencv` |
| `RESOLUTION` | Capture resolution actually negotiated with the camera |
| `FPS` | Capture frame rate |
| `DECODE` | Latency of the most recent decode |
| Log | Camera open, reconnect and backend fallback events, with timestamps |

### Shortcuts

| Key | Action |
| --- | --- |
| `Space` | Start / stop scanning |
| `D` | Toggle the diagnostics drawer |
| `Esc` | Close the open overlay, or stop scanning |

## Project layout

```
qr_scanner/
├── requirements.txt
├── README.md            # English
├── README.zh-CN.md      # 中文
└── src/
    ├── main.py          # app wiring: threads, stable-frame logic, shortcuts, beep
    ├── camera.py        # CameraThread: node discovery, capture, latest-frame slot
    ├── detector.py      # CodeDetector (zxing/zbar/OpenCV) + DetectionWorker
    ├── payload.py       # raw string -> structured Payload
    ├── preview.py       # ScannerView: full-screen frame + HUD painting
    └── hud.py           # top bar, result rail, detail overlay, diagnostics drawer
```

## Support

If you run into any issue while using this demo, feel free to post on the [Quectel official forum](https://forums.quectel.com/) — the Quectel support team will follow up.
