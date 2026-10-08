"""Brother QL print server.

FastAPI microservice for a Raspberry Pi (Zero 2 W and up) wired to a Brother QL
printer. Which printer, label and connection is set in config.py.
"""

import base64
import io
import json
import os
import re
import threading
import zipfile
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterator, List, Optional, Tuple
import cv2
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import numpy as np
from pdf2image import convert_from_bytes, pdfinfo_from_bytes
from pdf2image.exceptions import PDFPageCountError, PDFSyntaxError
from PIL import Image, ImageDraw

from brother_ql.backends.helpers import send
from brother_ql.conversion import convert
from brother_ql.raster import BrotherQLRaster

from . import API_VERSION, __version__
from .config import LABEL_DPI, device_path, find_label, load_settings

SETTINGS = load_settings()
LABEL = find_label(SETTINGS.label)

app = FastAPI(title="Brother QL Print Server", version=__version__)

# Working canvas: the label's printable area in dots (300 DPI)
CANVAS_WIDTH, CANVAS_HEIGHT = LABEL.dots_printable
MARGIN_X = 8
MARGIN_Y = 8
SAFE_WIDTH = CANVAS_WIDTH - (MARGIN_X * 2)
SAFE_HEIGHT = CANVAS_HEIGHT - (MARGIN_Y * 2)

MAX_COPIES = 50
MAX_LABELS = 100  # labels in one job, after unpacking zips and splitting PDFs
MAX_PRINTS = 500  # labels x copies in one job (a DK-11209 roll holds 800)
PRINT_CHUNK = 5  # copies per write to the printer; also the granularity of the progress report
MAX_FILE_BYTES = 25 * 1024 * 1024  # per upload and per zip member
MAX_ZIP_BYTES = 100 * 1024 * 1024  # total unpacked size of one zip
MAX_ZIP_MEMBERS = 200
MAX_PIXELS = 20_000_000  # larger bitmaps do not fit the Pi's RAM during preprocessing
PDF_DPI = LABEL_DPI
LABEL_MM = LABEL.tape_size
ASPECT_TOLERANCE = 0.15  # how far a source's proportions may stray from the label's before it is refused
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
LABEL_EXTENSIONS = IMAGE_EXTENSIONS + (".pdf",)
WEB_DIR = Path(__file__).parent / "web"
GAP_THRESHOLD = 10  # vertical gaps up to this height do not split a content block
MIN_GAP = 8  # smallest gap kept between repacked strips
MAX_SCALE = 2.0
MAX_GAP_RATIO = 1.0  # a gap never exceeds the typical block height, however much height is left over
QR_PAD = 6
QR_TEXT_GAP = 16  # minimum clearance between the text block and the QR code
# convert() binarizes at ~30% brightness, so anything lighter than this never
# prints (frames, shadows) and must not count as content
INK_THRESHOLD = 160

PRINTER_IDENTIFIER = SETTINGS.identifier
MODEL = SETTINGS.model


def printer_connected() -> Optional[bool]:
    """Whether the printer is there; None when it cannot be told cheaply (network and usb printers)."""
    device = device_path(PRINTER_IDENTIFIER)
    return None if device is None else os.path.exists(device)


class NoContentError(ValueError):
    """The image has nothing dark enough to print."""


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
        raise NoContentError("no printable content")

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

    # Each strip is cropped to its own ink, so a strip can be scaled by its own width limit
    strips = []
    for top, bottom in merged:
        ink_w = int(np.flatnonzero(thresh[top:bottom, min_x:max_x].any(axis=0)).max()) + 1
        strips.append(img.crop((min_x, top, min_x + ink_w, bottom)))
    gaps = len(strips) - 1

    # Reserve room for the gaps up front (at most half the area height) so
    # strips plus gaps can never spill out of the area
    min_gap = min(MIN_GAP, (area_h // 2) // gaps) if gaps else 0

    # Width caps are per strip: a long line shrinks to fit while the others keep
    # growing until the height is used. Find the largest common scale that fits.
    caps = [area_w / s.width for s in strips]
    budget = area_h - min_gap * gaps

    def total_height(common: float) -> float:
        return sum(s.height * min(common, cap) for s, cap in zip(strips, caps))

    common = MAX_SCALE
    if total_height(common) > budget:
        low, high = 0.0, MAX_SCALE
        for _ in range(40):
            mid = (low + high) / 2
            low, high = (mid, high) if total_height(mid) <= budget else (low, mid)
        common = low
    scales = [min(common, cap) for cap in caps]

    scaled_strips = [
        s.resize(
            (max(1, int(s.width * scale)), max(1, int(s.height * scale))),
            Image.Resampling.LANCZOS,
        )
        for s, scale in zip(strips, scales)
    ]

    free_h = area_h - sum(s.height for s in scaled_strips)
    gap_y = free_h // gaps if gaps else 0
    if gaps:
        # Spare height should not tear the lines apart: a gap never exceeds the typical block height
        typical = round(MAX_GAP_RATIO * sum(s.height for s in scaled_strips) / len(scaled_strips))
        gap_y = min(gap_y, max(min_gap, typical))
    cur_y = area_y + (free_h - gap_y * gaps) // 2

    # The block keeps the alignment of its widest line; lines stay left-aligned within it
    pos_x = area_x if align_left else area_x + (area_w - max(s.width for s in scaled_strips)) // 2
    for s in scaled_strips:
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
    except NoContentError:
        pass  # a label that is only a QR code is fine
    return canvas


class LabelError(Exception):
    """An upload that cannot become a label; `code` is mapped to a message by the web UI."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


# pdftoppm runs as a subprocess and OpenCV releases the GIL, so threads give real parallelism
# on the Pi's four cores; more workers than that only cost RAM
pool = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1))


@dataclass
class Job:
    labels: List[Tuple[str, Image.Image]] = field(default_factory=list)
    pending: List[Tuple[str, Future]] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)
    overflowed: bool = False

    @property
    def full(self) -> bool:
        return len(self.labels) + len(self.pending) >= MAX_LABELS

    def submit(self, name: str, work: Callable, *args) -> None:
        """Process a label on the pool; `collect` gathers the results in submission order."""
        self.pending.append((name, pool.submit(work, *args)))

    def results(self) -> Iterator[Tuple[str, Image.Image]]:
        """Yield finished labels in submission order as they complete; failures go to `errors`."""
        while self.pending:
            name, future = self.pending.pop(0)
            try:
                label = (name, future.result())
            except NoContentError as exc:
                self.fail(name, "no_content", str(exc))
                continue
            except (ValueError, OSError, PDFPageCountError, PDFSyntaxError, Image.DecompressionBombError) as exc:
                self.fail(name, "unreadable", str(exc))
                continue
            self.labels.append(label)
            yield label

    def collect(self) -> None:
        for _ in self.results():
            pass

    def fail(self, name: str, code: str, detail: str) -> None:
        self.errors.append({"name": name, "code": code, "detail": detail})

    def overflow(self, name: str) -> None:
        if not self.overflowed:
            self.overflowed = True
            self.fail(name, "too_many", f"more than {MAX_LABELS} labels in one job")


def natural_key(name: str) -> list:
    """Sort key that orders label-5 before label-10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def read_limited(stream, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise LabelError("too_large", f"larger than {limit // 2**20} MB")
    return data


def fits_label(width: float, height: float) -> bool:
    """True when the source has the label's proportions, in either orientation.

    Anything else (an A4 page, a photo) could only be cropped or squeezed onto
    62 x 29 mm, so it is refused instead of being made to fit.
    """
    if min(width, height) <= 0:
        return False
    wanted = max(LABEL_MM) / min(LABEL_MM)
    return abs(max(width, height) / min(width, height) / wanted - 1) <= ASPECT_TOLERANCE


def add_image(job: Job, name: str, content: bytes) -> None:
    try:
        source = Image.open(io.BytesIO(content))
        if source.width * source.height > MAX_PIXELS:
            raise LabelError("too_large", f"{source.width}x{source.height} px image")
        if not fits_label(source.width, source.height):
            raise LabelError("wrong_format", f"{source.width} × {source.height} px")
        job.submit(name, segment_and_repack, source)
    except LabelError as exc:
        job.fail(name, exc.code, str(exc))
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        job.fail(name, "unreadable", str(exc))


def render_page(content: bytes, page: int) -> Image.Image:
    # One page at a time: a whole document at 300 DPI exhausts the Pi's RAM
    image = convert_from_bytes(content, dpi=PDF_DPI, first_page=page, last_page=page)[0]
    return segment_and_repack(image)


def add_pdf(job: Job, name: str, content: bytes) -> None:
    try:
        info = pdfinfo_from_bytes(content)
        pages = int(info["Pages"])
        # Per-page sizes: with a page range pdfinfo reports "Page    N size"
        sizes = pdfinfo_from_bytes(content, first_page=1, last_page=pages)
        page_sizes = [
            tuple(map(float, re.findall(r"[\d.]+", value)[:2]))
            for key, value in sizes.items()
            if re.fullmatch(r"Page\s+\d+ size", key)
        ] or [tuple(map(float, re.findall(r"[\d.]+", info["Page size"])[:2]))]
    except (PDFPageCountError, PDFSyntaxError, KeyError, ValueError) as exc:
        job.fail(name, "unreadable", str(exc))
        return
    for width_pt, height_pt in page_sizes:
        if not fits_label(width_pt, height_pt):
            job.fail(name, "wrong_format", f"{width_pt / 72 * 25.4:.0f} × {height_pt / 72 * 25.4:.0f} mm")
            return
        if (width_pt / 72 * PDF_DPI) * (height_pt / 72 * PDF_DPI) > MAX_PIXELS:
            job.fail(name, "too_large", f"page size {width_pt:.0f}x{height_pt:.0f} pt")
            return

    for page in range(1, pages + 1):
        label = name if pages == 1 else f"{name} ({page}/{pages})"
        if job.full:
            job.overflow(label)
            return
        job.submit(label, render_page, content, page)


def add_zip(job: Job, name: str, content: bytes) -> None:
    """Unpack in memory (nothing touches the disk, so member paths cannot escape) and add each member."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = [m for m in archive.infolist() if not m.is_dir()]
    except zipfile.BadZipFile as exc:
        job.fail(name, "zip_unreadable", str(exc))
        return
    if len(members) > MAX_ZIP_MEMBERS:
        job.fail(name, "zip_limits", f"more than {MAX_ZIP_MEMBERS} entries")
        return

    unpacked = 0
    entries = [(PurePosixPath(m.filename.replace("\\", "/")), m) for m in members]
    for path, member in sorted(entries, key=lambda e: natural_key(e[0].name)):
        base = path.name
        # Archive clutter (macOS metadata, hidden files, nested zips, notes) is skipped, not an error
        if "__MACOSX" in path.parts or base.startswith(".") or not base.lower().endswith(LABEL_EXTENSIONS):
            continue
        if job.full:
            job.overflow(base)
            return
        try:
            with archive.open(member) as stream:
                data = read_limited(stream, min(MAX_FILE_BYTES, MAX_ZIP_BYTES - unpacked))
        except LabelError as exc:
            job.fail(base, exc.code if unpacked + MAX_FILE_BYTES <= MAX_ZIP_BYTES else "zip_limits", str(exc))
            continue
        except RuntimeError as exc:  # zipfile raises RuntimeError for encrypted members
            job.fail(base, "password", str(exc))
            continue
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
            job.fail(base, "unreadable", str(exc))
            continue
        unpacked += len(data)
        add_file(job, base, data)


def add_file(job: Job, name: str, content: bytes) -> None:
    lower = name.lower()
    if lower.endswith(".zip"):
        add_zip(job, name, content)
    elif lower.endswith(".pdf"):
        add_pdf(job, name, content)
    elif lower.endswith(IMAGE_EXTENSIONS):
        add_image(job, name, content)
    else:
        job.fail(name, "unsupported", "only PDF, PNG, JPG and ZIP are accepted")


def start_job(uploads: List[UploadFile]) -> Job:
    """Read and validate the uploads and queue every label; the results are not awaited yet."""
    job = Job()
    for upload in uploads:
        name = PurePosixPath((upload.filename or "file").replace("\\", "/")).name
        if job.full:
            job.overflow(name)
            break
        try:
            content = read_limited(upload.file, MAX_FILE_BYTES)
        except LabelError as exc:
            job.fail(name, exc.code, str(exc))
            continue
        add_file(job, name, content)
    return job


def build_job(uploads: List[UploadFile]) -> Job:
    job = start_job(uploads)
    job.collect()
    return job


def cut_plan(copies: int, cut_every: int, cut_at_end: bool) -> List[bool]:
    """Per-label cut flags: every `cut_every`-th label, and the last one per `cut_at_end`."""
    plan = [cut_every > 0 and i % cut_every == 0 for i in range(1, copies + 1)]
    plan[-1] = cut_at_end
    return plan


def build_instructions(img: Image.Image, cut_flags: List[bool]) -> bytes:
    instructions = b""
    for should_cut in cut_flags:
        # convert() returns the raster's whole accumulated data, so a shared
        # BrotherQLRaster would resend every earlier label with each new one
        qlr = BrotherQLRaster(MODEL)
        qlr.exception_on_warning = True
        instructions += convert(
            qlr=qlr,
            images=[img],
            label=SETTINGS.label,
            rotate="0",
            threshold=70.0,
            dither=False,
            compress=True,
            red=False,
            cut=should_cut,
        )
    return instructions


def dispatch_to_printer(
    labels: List[Tuple[str, Image.Image]],
    copies: int,
    cut_every: int,
    cut_at_end: bool,
    on_progress: Optional[Callable[[int], None]] = None,
) -> None:
    # The cut plan spans the whole job, so cut_every counts labels across files
    plan = iter(cut_plan(len(labels) * copies, cut_every, cut_at_end))
    printed = 0
    for _, img in labels:
        flags = [next(plan) for _ in range(copies)]
        for start in range(0, copies, PRINT_CHUNK):
            chunk = flags[start : start + PRINT_CHUNK]
            try:
                send(
                    instructions=build_instructions(img, chunk),
                    printer_identifier=PRINTER_IDENTIFIER,
                    backend_identifier=SETTINGS.backend,
                    blocking=True,
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Printer {PRINTER_IDENTIFIER} failed after {printed} labels: {exc}",
                ) from exc
            printed += len(chunk)
            if on_progress:
                on_progress(printed)


def png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# Progress of running print jobs, keyed by a client-chosen id, so the page can poll while its
# /print request is still in flight. In memory only; an entry lives as long as its request.
progress: Dict[str, dict] = {}
print_lock = threading.Lock()  # one job on the printer at a time


@app.get("/info")
async def handle_info():
    """Identity and capabilities, so a discovered service can be checked before use."""
    return {
        "service": "label-printer",
        "version": API_VERSION,
        "printer": {"model": MODEL, "connected": printer_connected()},
        "label": {"id": SETTINGS.label, "width_mm": LABEL_MM[0], "height_mm": LABEL_MM[1], "dpi": LABEL_DPI},
        "limits": {
            "max_copies": MAX_COPIES,
            "max_labels": MAX_LABELS,
            "max_prints": MAX_PRINTS,
            "max_file_mb": MAX_FILE_BYTES // 2**20,
        },
        "accepts": list(LABEL_EXTENSIONS) + [".zip"],
    }


@app.get("/progress/{job_id}")
async def handle_progress(job_id: str):
    return progress.get(job_id, {"stage": "unknown"})


# Plain `def`: FastAPI runs it in a worker thread, so the event loop stays free to answer /progress
@app.post("/print")
def handle_print(
    files: List[UploadFile] = File(...),
    copies: int = Form(1, ge=1, le=MAX_COPIES),
    cut_at_end: bool = Form(True),
    cut_every: int = Form(0, ge=0),
    selected: Optional[List[int]] = Form(None),
    job_id: Optional[str] = Form(None, pattern=r"^[A-Za-z0-9_-]{8,64}$"),
):
    try:
        return run_print(files, copies, cut_at_end, cut_every, selected, job_id)
    finally:
        progress.pop(job_id, None)


def run_print(files, copies, cut_at_end, cut_every, selected, job_id):
    def track(**fields) -> None:
        if job_id:
            progress[job_id] = fields

    if printer_connected() is False:
        raise HTTPException(
            status_code=503,
            detail=f"Printer {PRINTER_IDENTIFIER} is not connected.",
        )

    track(stage="processing")
    job = build_job(files)
    if job.errors or not job.labels:
        return JSONResponse(
            status_code=400,
            content={"detail": "unusable files, nothing was printed", "errors": job.errors},
        )
    labels = job.labels
    if selected is not None:
        # Indices into the label order /preview returned for the same files
        valid = selected and len(set(selected)) == len(selected) and all(0 <= i < len(labels) for i in selected)
        if not valid:
            return JSONResponse(
                status_code=400,
                content={"detail": "invalid label selection", "errors": []},
            )
        labels = [labels[i] for i in sorted(selected)]
    total = len(labels) * copies
    if total > MAX_PRINTS:
        return JSONResponse(
            status_code=400,
            content={"detail": f"{total} labels exceeds the limit of {MAX_PRINTS}", "errors": []},
        )

    with print_lock:
        track(stage="printing", done=0, total=total)
        dispatch_to_printer(
            labels, copies, cut_every, cut_at_end,
            on_progress=lambda done: track(stage="printing", done=done, total=total),
        )
    return {
        "status": "ok",
        "labels": len(labels),
        "copies": copies,
        "printed": total,
        "cut_at_end": cut_at_end,
        "cut_every": cut_every,
    }


@app.post("/preview")
def handle_preview(files: List[UploadFile] = File(...)):
    job = build_job(files)
    encoded = pool.map(png_data_url, (img for _, img in job.labels))
    return {
        "labels": [{"name": name, "png": png} for (name, _), png in zip(job.labels, encoded)],
        "errors": job.errors,
    }


@app.post("/preview/stream")
def handle_preview_stream(files: List[UploadFile] = File(...)):
    """Like /preview, but as newline-delimited JSON, so the page can show labels as they finish:
    `{"total": n}`, then one `{"label": {name, png}}` per label in order, then `{"errors": [...]}`."""
    job = start_job(files)  # reads the uploads here, before the request's files are closed
    total = len(job.pending)

    def events() -> Iterator[str]:
        yield json.dumps({"total": total}) + "\n"
        for name, img in job.results():
            yield json.dumps({"label": {"name": name, "png": png_data_url(img)}}) + "\n"
        yield json.dumps({"errors": job.errors}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/", response_class=FileResponse)
async def web_ui():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
