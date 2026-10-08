"""Brother QL-600 print server.

FastAPI microservice for a Raspberry Pi Zero 2 W wired to a Brother QL-600
loaded with DK-11209 labels (62 x 29 mm).
"""

import io
import os
from typing import List, Optional, Tuple
import cv2
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
import numpy as np
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFPageCountError, PDFSyntaxError
from PIL import Image, ImageDraw

from brother_ql.backends.helpers import send
from brother_ql.conversion import convert
from brother_ql.raster import BrotherQLRaster

app = FastAPI(title="Brother QL-600 Print Server")

# Working canvas (300 DPI, DK-11209 62x29 mm label)
CANVAS_WIDTH = 696
CANVAS_HEIGHT = 271
MARGIN_X = 8
MARGIN_Y = 8
SAFE_WIDTH = CANVAS_WIDTH - (MARGIN_X * 2)
SAFE_HEIGHT = CANVAS_HEIGHT - (MARGIN_Y * 2)

MAX_COPIES = 50
GAP_THRESHOLD = 10  # vertical gaps up to this height do not split a content block
MIN_GAP = 8  # smallest gap kept between repacked strips
MAX_SCALE = 2.0
QR_PAD = 6
QR_TEXT_GAP = 16  # minimum clearance between the text block and the QR code
# convert() binarizes at ~30% brightness, so anything lighter than this never
# prints (frames, shadows) and must not count as content
INK_THRESHOLD = 160

PRINTER_DEVICE = os.getenv("PRINTER_DEVICE", "/dev/usb/lp0")
MODEL = os.getenv("PRINTER_MODEL", "QL-600")


def flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Composite any transparency onto white so it is not read as black content."""
    rgba = img.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert("RGB")


def find_qr(gray: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """Tight bounding box (x, y, w, h) of a QR code in the image, or None."""
    found, points = cv2.QRCodeDetector().detect(gray)
    if not found:
        return None
    x, y, w, h = cv2.boundingRect(points.astype(np.int32))
    # The detector's corners can land a pixel inside the symbol, so take the
    # box from the ink in a slightly larger window
    x0, y0 = max(0, x - QR_PAD), max(0, y - QR_PAD)
    ys, xs = np.nonzero(gray[y0 : y + h + QR_PAD, x0 : x + w + QR_PAD] < INK_THRESHOLD)
    if xs.size == 0:
        return None
    return (
        x0 + int(xs.min()),
        y0 + int(ys.min()),
        int(xs.max() - xs.min()) + 1,
        int(ys.max() - ys.min()) + 1,
    )


def render_qr(crop: Image.Image, size: int) -> Image.Image:
    """Redraw a QR code at `size` px from its module grid, so modules stay uniform and crisp.

    Resampling the bitmap smears modules whenever the scale is not a whole
    number, and some codes stop decoding. Falls back to a plain nearest-neighbour
    resize if the grid cannot be read.
    """
    ink = np.array(crop.convert("L")) < 128
    top = ink[min(2, ink.shape[0] - 1)]
    start = int(np.argmax(top))
    run = len(top) - start if top[start:].all() else int(np.argmin(top[start:]))
    if top.any() and run > 0:
        # The first black run on the top row is the 7-module finder pattern;
        # QR sizes are 21 + 4k modules
        modules = 21 + 4 * max(0, round((ink.shape[1] / (run / 7) - 21) / 4))
        rows = ((np.arange(modules) + 0.5) * ink.shape[0] / modules).astype(int)
        cols = ((np.arange(modules) + 0.5) * ink.shape[1] / modules).astype(int)
        ink = ink[np.ix_(rows, cols)]
    grid = Image.fromarray(np.where(ink, 0, 255).astype(np.uint8))
    return grid.resize((size, size), Image.Resampling.NEAREST).convert("RGB")


def layout_strips(
    canvas: Image.Image,
    img: Image.Image,
    area: Tuple[int, int, int, int],
    align_left: bool,
) -> None:
    """Crop dead margins, split content into strips and rebalance them vertically inside `area`.

    Raises ValueError when the image has no printable content.
    """
    area_x, area_y, area_w, area_h = area
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, INK_THRESHOLD, 255, cv2.THRESH_BINARY_INV)

    # Join neighbouring glyphs horizontally so a text line becomes one contour
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    dilated = cv2.dilate(thresh, kernel, iterations=2)

    contours, _ = cv2.findContours(
        dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    boxes = [b for b in map(cv2.boundingRect, contours) if b[2] > 4 and b[3] > 4]
    if not boxes:
        raise ValueError("no printable content")

    # Dilation inflates the boxes; take the bounds from the ink itself
    ink = np.zeros_like(thresh)
    for x, y, w, h in boxes:
        ink[y : y + h, x : x + w] = thresh[y : y + h, x : x + w]
    ys, xs = np.nonzero(ink)
    min_x, max_x = int(xs.min()), int(xs.max()) + 1
    min_y, max_y = int(ys.min()), int(ys.max()) + 1

    # Y-axis ink profile: rows without ink are gaps between blocks
    y_profile = np.any(thresh[min_y:max_y, min_x:max_x], axis=1)
    segments: List[Tuple[int, int]] = []
    in_block = False
    start_y = 0

    for idx, has_content in enumerate(y_profile):
        actual_y = min_y + idx
        if has_content and not in_block:
            in_block = True
            start_y = actual_y
        elif not has_content and in_block:
            in_block = False
            segments.append((start_y, actual_y))
    if in_block:
        segments.append((start_y, max_y))

    # Only gaps taller than GAP_THRESHOLD separate blocks; smaller ones (line spacing) stay inside
    merged: List[Tuple[int, int]] = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] <= GAP_THRESHOLD:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)

    strips = [img.crop((min_x, s[0], max_x, s[1])) for s in merged]
    gaps = len(strips) - 1

    # Reserve room for the gaps up front (at most half the area height) so
    # strips plus gaps can never spill out of the area
    min_gap = min(MIN_GAP, (area_h // 2) // gaps) if gaps else 0
    scale = min(
        area_w / (max_x - min_x),
        (area_h - min_gap * gaps) / sum(s.height for s in strips),
        MAX_SCALE,
    )

    scaled_strips = [
        s.resize(
            (max(1, int(s.width * scale)), max(1, int(s.height * scale))),
            Image.Resampling.LANCZOS,
        )
        for s in strips
    ]

    free_h = area_h - sum(s.height for s in scaled_strips)
    gap_y = free_h // gaps if gaps else 0
    cur_y = area_y + (free_h - gap_y * gaps) // 2

    for s in scaled_strips:
        pos_x = area_x if align_left else area_x + (area_w - s.width) // 2
        canvas.paste(s, (pos_x, cur_y))
        cur_y += s.height + gap_y


def segment_and_repack(img: Image.Image) -> Image.Image:
    """Compose the label: QR code (if any) full height at the right edge, rest repacked beside it.

    Raises ValueError when the image has no printable content.
    """
    img = flatten_to_rgb(img)
    if img.height > img.width:
        img = img.rotate(90, expand=True)

    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    text_area = (MARGIN_X, MARGIN_Y, SAFE_WIDTH, SAFE_HEIGHT)

    qr_box = find_qr(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY))
    if qr_box is None:
        layout_strips(canvas, img, text_area, align_left=False)
        return canvas

    x, y, w, h = qr_box
    qr = img.crop((x, y, x + w, y + h))
    # Blank the QR out so only the text is laid out beside it
    ImageDraw.Draw(img).rectangle((x, y, x + w, y + h), fill=(255, 255, 255))

    # Square at full safe height, flush right
    qr_size = SAFE_HEIGHT
    qr = render_qr(qr, qr_size)
    qr_x = CANVAS_WIDTH - MARGIN_X - qr_size
    canvas.paste(qr, (qr_x, MARGIN_Y))

    text_w = qr_x - QR_TEXT_GAP - MARGIN_X
    try:
        layout_strips(
            canvas, img, (MARGIN_X, MARGIN_Y, text_w, SAFE_HEIGHT), align_left=True
        )
    except ValueError:
        pass  # a label that is only a QR code is fine
    return canvas


def cut_plan(copies: int, cut_every: int, cut_at_end: bool) -> List[bool]:
    """Per-label cut flags: every `cut_every`-th label, and the last one per `cut_at_end`."""
    plan = [cut_every > 0 and i % cut_every == 0 for i in range(1, copies + 1)]
    plan[-1] = cut_at_end
    return plan


def build_instructions(
    img: Image.Image, copies: int, cut_every: int, cut_at_end: bool
) -> bytes:
    instructions = b""
    for should_cut in cut_plan(copies, cut_every, cut_at_end):
        # convert() returns the raster's whole accumulated data, so a shared
        # BrotherQLRaster would resend every earlier label with each new one
        qlr = BrotherQLRaster(MODEL)
        qlr.exception_on_warning = True
        instructions += convert(
            qlr=qlr,
            images=[img],
            label="62x29",
            rotate="0",
            threshold=70.0,
            dither=False,
            compress=True,
            red=False,
            cut=should_cut,
        )
    return instructions


def dispatch_to_printer(
    img: Image.Image,
    copies: int = 1,
    cut_at_end: bool = True,
    cut_every: int = 0,
):
    instructions = build_instructions(img, copies, cut_every, cut_at_end)
    try:
        send(
            instructions=instructions,
            printer_identifier=f"file://{PRINTER_DEVICE}",
            backend_identifier="linux_kernel",
            blocking=True,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail=f"Printer {PRINTER_DEVICE} unavailable: {exc}"
        ) from exc


def render_label(content: bytes, filename: Optional[str]) -> Image.Image:
    """Decode an upload (PDF or image) and run the repack pipeline; 400 on unusable input."""
    try:
        if filename and filename.lower().endswith(".pdf"):
            # First page only: rendering a whole A4 document at 300 DPI exhausts the Pi's RAM
            pages = convert_from_bytes(content, dpi=300, first_page=1, last_page=1)
            if not pages:
                raise ValueError("empty PDF")
            source = pages[0]
        else:
            source = Image.open(io.BytesIO(content))
        return segment_and_repack(source)
    except (
        ValueError,
        OSError,
        PDFPageCountError,
        PDFSyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise HTTPException(status_code=400, detail=f"Unusable file: {exc}") from exc


# ponytail: handlers are async but do blocking work, which deliberately serializes
# jobs on the 512 MB Pi; move to threads plus a lock if concurrent clients matter
@app.post("/print")
async def handle_print(
    file: UploadFile = File(...),
    copies: int = Form(1, ge=1, le=MAX_COPIES),
    cut_at_end: bool = Form(True),
    cut_every: int = Form(0, ge=0),
):
    if not os.path.exists(PRINTER_DEVICE):
        raise HTTPException(
            status_code=503,
            detail=f"Printer {PRINTER_DEVICE} is not connected.",
        )

    processed = render_label(await file.read(), file.filename)
    dispatch_to_printer(
        processed, copies=copies, cut_at_end=cut_at_end, cut_every=cut_every
    )
    return {
        "status": "ok",
        "copies": copies,
        "cut_at_end": cut_at_end,
        "cut_every": cut_every,
    }


@app.post("/preview")
async def handle_preview(file: UploadFile = File(...)):
    processed = render_label(await file.read(), file.filename)
    buf = io.BytesIO()
    processed.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    return """
    <!DOCTYPE html>
    <html lang="pl">
    <head>
        <meta charset="utf-8">
        <title>Brother QL-600 Print Server</title>
        <style>
            body { font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #eceff1; margin: 0; }
            .card { background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); width: 420px; }
            h2 { margin-top: 0; }
            label { display: block; margin-top: 1rem; font-size: 0.9rem; color: #333; }
            input, select, button { width: 100%; box-sizing: border-box; margin-top: 0.3rem; }
            .checkbox-group { display: flex; align-items: center; gap: 8px; margin-top: 1rem; }
            .checkbox-group input { width: auto; margin: 0; }
            button { padding: 10px; background: #0288d1; border: none; color: white; border-radius: 4px; font-weight: bold; cursor: pointer; margin-top: 1.5rem; }
            button:hover { background: #0277bd; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Drukarka Etykiet 62x29</h2>
            <form action="/print" method="post" enctype="multipart/form-data">
                <label>Plik etykiety (PDF / PNG):
                    <input type="file" name="file" accept=".pdf,image/*" required>
                </label>
                
                <label>Liczba kopii:
                    <input type="number" name="copies" value="1" min="1" max="50">
                </label>

                <label>Cięcie pośrednie (Cut between):
                    <select name="cut_every">
                        <option value="0" selected>Wyłączone (OFF - jeden pasek)</option>
                        <option value="1">Każda etykieta (co 1)</option>
                        <option value="2">Co 2 etykiety</option>
                        <option value="3">Co 3 etykiety</option>
                        <option value="4">Co 4 etykiety</option>
                        <option value="5">Co 5 etykiet</option>
                    </select>
                </label>

                <div class="checkbox-group">
                    <input type="checkbox" id="cut_at_end" name="cut_at_end" value="true" checked>
                    <label for="cut_at_end" style="margin: 0;">Odetnij na końcu serii (Cut at end)</label>
                </div>

                <button type="submit">Drukuj</button>
            </form>
        </div>
    </body>
    </html>
    """
