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
from PIL import Image

from brother_ql.backends.helpers import send
from brother_ql.conversion import convert
from brother_ql.raster import BrotherQLRaster

app = FastAPI(title="Brother QL-600 Print Server")

# Working canvas (300 DPI, DK-11209 62x29 mm label)
CANVAS_WIDTH = 696
CANVAS_HEIGHT = 271
MARGIN_X = 24
MARGIN_Y = 24
SAFE_WIDTH = CANVAS_WIDTH - (MARGIN_X * 2)
SAFE_HEIGHT = CANVAS_HEIGHT - (MARGIN_Y * 2)

MAX_COPIES = 50
GAP_THRESHOLD = 10  # vertical gaps up to this height do not split a content block
MIN_GAP = 8  # smallest gap kept between repacked strips
MAX_SCALE = 2.0

PRINTER_DEVICE = os.getenv("PRINTER_DEVICE", "/dev/usb/lp0")
MODEL = os.getenv("PRINTER_MODEL", "QL-600")


def flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Composite any transparency onto white so it is not read as black content."""
    rgba = img.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert("RGB")


def segment_and_repack(img: Image.Image) -> Image.Image:
    """Crop dead margins, split content into strips and rebalance them vertically.

    Raises ValueError when the image has no printable content.
    """
    img = flatten_to_rgb(img)
    if img.height > img.width:
        img = img.rotate(90, expand=True)

    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY_INV)

    # Join neighbouring glyphs horizontally so a text line becomes one contour
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    dilated = cv2.dilate(thresh, kernel, iterations=2)

    contours, _ = cv2.findContours(
        dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    boxes = [b for b in map(cv2.boundingRect, contours) if b[2] > 4 and b[3] > 4]
    if not boxes:
        raise ValueError("no printable content")

    min_x = min(b[0] for b in boxes)
    max_x = max(b[0] + b[2] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_y = max(b[1] + b[3] for b in boxes)

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

    # Reserve room for the gaps up front (at most half the safe height) so
    # strips plus gaps can never spill into the cutter margin
    min_gap = min(MIN_GAP, (SAFE_HEIGHT // 2) // gaps) if gaps else 0
    scale = min(
        SAFE_WIDTH / (max_x - min_x),
        (SAFE_HEIGHT - min_gap * gaps) / sum(s.height for s in strips),
        MAX_SCALE,
    )

    scaled_strips = [
        s.resize(
            (max(1, int(s.width * scale)), max(1, int(s.height * scale))),
            Image.Resampling.LANCZOS,
        )
        for s in strips
    ]

    scaled_total_h = sum(s.height for s in scaled_strips)
    free_h = SAFE_HEIGHT - scaled_total_h
    gap_y = free_h // gaps if gaps else 0
    cur_y = MARGIN_Y + (free_h - gap_y * gaps) // 2

    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    for s in scaled_strips:
        pos_x = MARGIN_X + (SAFE_WIDTH - s.width) // 2
        canvas.paste(s, (pos_x, cur_y))
        cur_y += s.height + gap_y

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
