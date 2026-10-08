# AGENTS.md — Context and Development Guidelines

This document outlines the architecture, hardware constraints, image-processing algorithms, and coding conventions for AI agents working on the **`brother-ql-print-server`** codebase.

---

## 1. Project Overview & Scope

`brother-ql-print-server` is an automated, headless network microservice built with **FastAPI** designed to run on a **Raspberry Pi Zero 2 W** connected directly via USB to a **Brother QL-600** (or QL-600B) thermal label printer.

Key objectives:
1. **Job Ingestion:** Accept label documents (PDF, PNG, JPG) via HTTP API and an integrated web UI.
2. **Intelligent Image Preprocessing (OpenCV + Pillow):**
   - Auto-orientation to landscape.
   - Autocrop (stripping out white, dead margins).
   - Y-axis density profiling to detect gaps between text/graphic blocks.
   - Smart vertical repack and rebalancing to fill label real estate proportionally without distortion.
3. **Electric Guillotine Cutter Management:**
   - Cut at job end (`cut_at_end`).
   - Intermediate cut every *N* labels (`cut_every`).
4. **Direct Raster Printing:** Generate native raster instructions using `brother_ql_inventree` (a maintained fork of `brother_ql`, still imported as `brother_ql`; upstream has no QL-600 model) and pipe them to `/dev/usb/lp0`.

---

## 2. Hardware & Raster Dimensions

| Parameter | Value / Detail |
|---|---|
| **Host** | Raspberry Pi Zero 2 W (Raspberry Pi OS 64-bit, 512 MB RAM) |
| **Printer** | Brother QL-600 / QL-600B |
| **Interface** | USB OTG Host -> `/dev/usb/lp0` (or directly via `pyusb`) |
| **Print Head** | 300 DPI thermal |
| **Media** | Die-cut labels **DK-11209** (62 × 29 mm, 800 labels/roll) |
| **Full Canvas (300 DPI)** | `CANVAS_WIDTH = 696 px`, `CANVAS_HEIGHT = 271 px` |
| **Safe Margins** | `MARGIN_X = 24 px` (~2.0 mm), `MARGIN_Y = 24 px` (~2.0 mm) |
| **Safe Printable Area** | `SAFE_WIDTH = 648 px`, `SAFE_HEIGHT = 223 px` |

> **Critical constraint:** No content must exceed the `Safe Area`. The `MARGIN_Y = 24 px` margin prevents the electric cutter from clipping label text or graphics.

---

## 3. Architecture & Image Pipeline

```
[Client: Web UI / cURL / Automation script]
                    │
                    ▼  POST /print (file + copies + cutter options)
┌──────────────────────────────────────────────────────────────┐
│  FastAPI (server.py)                                         │
│                                                              │
│  1. Ingest: PDF (pdf2image @ 300 DPI) or PIL image           │
│  2. Orientation: Auto-rotate if height > width               │
│  3. OpenCV Pipeline (`segment_and_repack`):                  │
│     - Threshold binarization (invert background, thresh=230) │
│     - Morphology: Horizontal dilation (kernel 15x3)          │
│     - Contour detection & bounding boxes                     │
│     - Y-axis density profile (find gaps > 10px)              │
│     - Slice content into horizontal strips                   │
│     - Proportional scaling (clamped to max scale 2.0)        │
│     - Uniform redistribution of vertical gaps (gap_y)        │
│  4. Composition: Paste strips centered onto 696x271 canvas   │
│  5. Cutter Control:                                          │
│     - cut_at_end: bool (default: True)                       │
│     - cut_every: int (0 = OFF / 1 = each label / N = every N)│
│  6. Driver Dispatch: brother_ql raster -> /dev/usb/lp0       │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. API Endpoints

- **`POST /print`**
  - `multipart/form-data`:
    - `file`: Label file (`.pdf`, `.png`, `.jpg`).
    - `copies`: Integer (`1` to `50`, default: `1`).
    - `cut_at_end`: Boolean flag (default: `True`).
    - `cut_every`: Intermediate cut interval (`0` = OFF, `1` = every label, `N` = every N labels).
  - Responses: `200 OK` (JSON) or `503 Service Unavailable` if printer device `/dev/usb/lp0` is missing.
- **`POST /preview`**
  - Form field `file`.
  - Response: Rendered PNG (696 × 271 px) reflecting the exact layout that would be printed.
- **`GET /`**
  - Minimalist drag-and-drop web interface for testing and ad-hoc printing from a browser.

---

## 5. Guidelines for AI Agents

### 5.1 Hardware Constraints (RPi Zero 2 W)
- **Memory limit: 512 MB RAM.** Avoid memory-intensive libraries or holding multiple raw image buffers uncollected.
- **OpenCV package:** Always use `opencv-python-headless` to omit heavy GUI/X11 dependencies.
- Explicitly close or free `BytesIO` buffers and temporary resources.
- Render only the first PDF page; a full A4 page at 300 DPI is already ~26 MB.

### 5.2 Operating System & Permissions
- Host requires system packages: `poppler-utils` (for `pdf2image`), `libgl1`, `libglib2.0-0`.
- User must be in group `lp` (`sudo usermod -a -G lp $USER`) for write access to `/dev/usb/lp0`.
- Environment variable overrides:
  - `PRINTER_DEVICE`: Default `/dev/usb/lp0`
  - `PRINTER_MODEL`: Default `QL-600`

### 5.3 Code Structure & Refactoring
- Current `server.py` and `requirements.txt` serve as the **initial starter reference implementation** to verify physical hardware and driver functionality. Cutting is emitted per label (one raster job each, cut flag from `cut_plan`); how the printer chains uncut labels still needs verifying on hardware.
- Future refactoring should modularize functionality:
  - `pipeline/`: Image processing, segmentation, and repacking.
  - `printer/`: Hardware communication and `brother_ql` raster generation.
  - `api/`: FastAPI route handlers and request models.
  - `web/`: Templates/static UI assets.
- Automated tests for image segmentation (`segment_and_repack`) must run independently without requiring a physical printer attached (e.g. using synthetic images / test fixtures). They live in `tests/` and run with `pytest`.

---

## 6. Development & Deployment

### Local Development:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### Production Systemd Service:
File: `/etc/systemd/system/label-printer.service`
```ini
[Unit]
Description=Brother QL-600 Print Server
After=network.target

[Service]
Type=simple
User=pi
Group=lp
WorkingDirectory=/home/pi/ql-printer-server
ExecStart=/home/pi/ql-printer-server/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now label-printer.service
```
