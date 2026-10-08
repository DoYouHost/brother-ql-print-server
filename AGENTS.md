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
| **Printer** | Brother QL-600 / QL-600B (tested); other QL models are configurable but untested |
| **Interface** | USB OTG Host -> `/dev/usb/lp0` (or directly via `pyusb`) |
| **Print Head** | 300 DPI thermal |
| **Media** | Landscape die-cut labels, default **DK-11209** (62 × 29 mm, 800 labels/roll); also 54x29, 52x29 and 102x51 (QL-1xxx) |
| **Full Canvas (300 DPI)** | The label's `dots_printable` from brother_ql: 696 × 271 px for 62x29 (`CANVAS_WIDTH`, `CANVAS_HEIGHT`) |
| **Safe Margins** | `MARGIN_X = 8 px` (~0.7 mm), `MARGIN_Y = 8 px` (~0.7 mm), inside the canvas |
| **Safe Printable Area** | Canvas minus the margins: 680 × 255 px for 62x29 |

> **Critical constraint:** No content must exceed the `Safe Area`. The 696 × 271 canvas is already the driver's full imageable area for 62x29 (Brother's PPD: 4.32 8.4 171.36 73.44 pt), so the physical ~1.5 mm side / ~3 mm top-bottom unprintable strips are not ours to spend; the extra 8 px only absorbs feed registration drift.

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
│     - Per-block scaling: one common scale (max 2.0) fills the│
│       height; a block wider than the area shrinks on its own │
│     - Redistribution of spare height into gaps (gap_y), each │
│       capped at the typical block height; block centred      │
│  4. Composition: a QR code (found via cv2.QRCodeDetector) is │
│     redrawn from its module grid, full safe height, flush    │
│     right; the remaining text is repacked left of it (gaps   │
│     capped at the typical block height). Without             │
│     a QR, strips are centered onto the 696x271 canvas        │
│  5. Cutter Control:                                          │
│     - cut_at_end: bool (default: True)                       │
│     - cut_every: int (0 = OFF / 1 = each label / N = every N)│
│  6. Driver Dispatch: brother_ql raster -> /dev/usb/lp0       │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. API Endpoints

A job is any number of uploads of mixed type. Sources must have the label's proportions (62 : 29, either orientation, within 15%); anything else (an A4 page, a photo) is refused with `wrong_format` and the size found, never cropped or squeezed onto the label. A PDF is refused as a whole if any page is off, before it is rendered.

Uploads: `.pdf` (every page is a label), `.png`, `.jpg`/`.jpeg`, and `.zip` (unpacked in memory; members are sorted naturally, hidden files, `__MACOSX`, nested zips and non-label files are skipped).

- **`POST /print`**
  - `multipart/form-data`:
    - `files`: One or more label files (repeat the field).
    - `copies`: Copies of *each* label (`1` to `50`, default: `1`).
    - `cut_at_end`: Boolean (default: `True`).
    - `cut_every`: Cut after every N-th label counted across the whole job (`0` = OFF, `1` = every label).
    - `selected`: Optional, repeatable. Indices into the label order `/preview` returned for the same files; only those labels print (omitted = all). The cut plan then spans just the selection.
    - `job_id`: Optional, `[A-Za-z0-9_-]{8,64}`, chosen by the client. While the request runs, `GET /progress/{job_id}` returns `{stage: processing}` and then `{stage: printing, done, total}` (labels x copies sent so far); unknown or finished ids return `{stage: unknown}`. State is in memory and lives only as long as the request.
  - Responses: `200 OK` (JSON with `labels`, `copies`, `printed`), `400` with an `errors` list (`name`, `code`, `detail`) if any file or page is unusable (nothing is printed), `503` if `/dev/usb/lp0` is missing or the printer fails mid-job (the detail says how many labels were sent).
  - Printing runs in a worker thread behind a lock (one job on the printer at a time) and sends copies in chunks of 5, so progress is smooth.
  - Limits: 100 labels per job, 500 labels x copies, 25 MB per upload / zip member, 100 MB unpacked per zip, 20 MP per image or PDF page.
- **`POST /preview`**
  - Form field `files` (same as above).
  - Response: JSON `{labels: [{name, png}], errors: [...]}`, where `png` is a data URL of the exact 696 x 271 layout that would be printed.
- **`GET /info`**
  - Identity and capabilities: `{service: "label-printer", version, printer: {model, connected}, label: {id, width_mm, height_mm, dpi}, limits, accepts}`. `version` is `API_VERSION` (bump on breaking changes).
- **Discovery (mDNS / DNS-SD)**
  - The Pi announces `_labelprinter._tcp` on the configured port through Avahi, with TXT `v`, `path=/info`, `model`, `label`, `dpi`. `python -m label_printer announce install|remove` writes or deletes `/etc/avahi/services/label-printer.service`, generated from the settings; the systemd unit runs it as root around the service, so the announcement exists only while the server runs.
  - A client should still call `GET /info` after resolving and check `printer.connected` (`null` for network and usb printers, which cannot be probed). Prefer the resolved IP over `rpi-label-printer.local`; Android does not resolve `.local` names reliably.
  - mDNS does not cross VLANs or guest Wi-Fi isolation; clients need a manual address fallback.
  - The API has no authentication: anyone on the LAN can print.
- **`GET /`**
  - English web UI (`web/index.html`) built on the Bambuddy Design System (tokens in `web/tokens.css`, copied 1:1 from the design system project; Manrope and JetBrains Mono self-hosted in `web/fonts/` so it works offline). Static files are served under `/web`.
  - Flow: choosing files starts the preview automatically (debounced, stale responses ignored); every label has a checkbox (all checked by default, deselections survive a refreshed preview); Print is enabled only for a preview without errors and sends the checked indices as `selected`.
  - Design rules to keep: no shadows or blur, depth from translucent layers and hairlines, green accent, JetBrains Mono for every number, sentence-case terse English copy without emoji.

---

## 5. Guidelines for AI Agents

### 5.1 Hardware Constraints (RPi Zero 2 W)
- **Memory limit: 512 MB RAM.** Avoid memory-intensive libraries or holding multiple raw image buffers uncollected.
- **OpenCV package:** Always use `opencv-python-headless` to omit heavy GUI/X11 dependencies.
- Explicitly close or free `BytesIO` buffers and temporary resources.
- Render only the first PDF page; a full A4 page at 300 DPI is already ~26 MB.

### 5.2 Operating System & Permissions
- Host requires system packages: `poppler-utils` (for `pdf2image`), `libgl1`, `libglib2.0-0`, `avahi-daemon`. The package declares them.
- The service user must be in group `lp` (owner of `/dev/usb/lp*`). The package creates `label-printer` in `lp`.
- Settings come from the environment (`/etc/default/label-printer` when installed; see `deploy/default`), validated at start by `label_printer/config.py`, which refuses a wrong value with a message that says what to change:
  - `PRINTER_MODEL`: default `QL-600`; any model brother_ql knows.
  - `PRINTER_LABEL`: default `62x29`; only landscape die-cut labels (the layout puts text left of a full-height QR code), and wide ones only on QL-1xxx.
  - `PRINTER_IDENTIFIER`: default `file:///dev/usb/lp0`; `tcp://host:9100` for network printers, `usb://0x04f9:0x20c0` for libusb. `PRINTER_DEVICE=/dev/usb/lp0` still works as the older spelling.
  - `LABEL_PRINTER_HOST`, `LABEL_PRINTER_PORT`: default `0.0.0.0:8000`.
- Only the QL-600 over USB is tested on hardware. Network, libusb and other models are wired through the settings but unverified.

### 5.3 Code Structure
- `label_printer/` is the package: `config.py` (settings and label tables), `server.py` (image pipeline, job building, printing, FastAPI routes), `announce.py` (Avahi file), `__main__.py` (`python -m label_printer [serve|check|announce]`), `web/` (UI and design tokens). `server.py` is still one module; splitting it into pipeline / printer / api is possible later.
- Cutting is emitted per label (one raster job each, cut flag from `cut_plan`); how the printer chains uncut labels still needs verifying on hardware.
- Automated tests must run without a printer (synthetic images, a fake `send`). They live in `tests/` and run with `pytest` (dev dependencies: `pytest`, `httpx`).

---

## 6. Development & Deployment

### Local development
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt pytest httpx
python -m label_printer check      # validates the settings and looks for the printer
python -m label_printer            # serves on 0.0.0.0:8000
pytest
```

### Package (the supported way to install)
`deploy/build-deb.sh` builds `dist/label-printer_<version>_<arch>.deb` for the machine it runs on (the bundled virtualenv holds compiled wheels, so build on the target architecture and Debian release, e.g. on a Pi). Installing it is `sudo apt install ./label-printer_0.1.0_arm64.deb`; nothing else needs configuring. The package:
- installs the code and a virtualenv into `/opt/label-printer/`,
- creates the system user `label-printer` in group `lp`,
- installs and starts `label-printer.service` (`deploy/label-printer.service`), which announces the service over mDNS while it runs,
- installs `/etc/default/label-printer` as a conffile (settings survive upgrades and `remove`; `purge` deletes everything).

`deploy/test-install.sh` verifies a build in a clean Debian 13 container (build, install, announcement, run as the service user, `/info`, remove, purge):
```bash
docker run --rm -v "$PWD":/src:ro debian:13 sh /src/deploy/test-install.sh
```
Releases are plain `.deb` files; bump `__version__` in `label_printer/__init__.py` first.

### Moving a manual install to the package
A hand-made install (`~/ql-printer-server`, unit `/etc/systemd/system/label-printer.service`, a copy of the Avahi file) overrides the package's unit of the same name: stop and disable it, delete the unit and `/etc/avahi/services/label-printer.service`, run `sudo systemctl daemon-reload`, then install the package.
