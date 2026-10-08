"""Pipeline, batch-upload and cut-plan checks; synthetic images, no printer needed.

Dev dependencies: pytest and httpx (FastAPI TestClient).
"""

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

import io
import zipfile

from fastapi.testclient import TestClient

from label_printer import server


def label_with_bars(count: int, bar_h: int = 40, gap: int = 30) -> Image.Image:
    img = Image.new("RGB", (900, 120 + count * (bar_h + gap)), "white")
    draw = ImageDraw.Draw(img)
    for i in range(count):
        y = 60 + i * (bar_h + gap)
        draw.rectangle((100, y, 800, y + bar_h), fill="black")
    return img


@pytest.mark.parametrize("count", [1, 2, 5, 12])
def test_content_stays_inside_safe_area(count):
    out = server.segment_and_repack(label_with_bars(count))
    assert out.size == (server.CANVAS_WIDTH, server.CANVAS_HEIGHT)
    left, top, right, bottom = out.convert("L").point(lambda v: 255 - v).getbbox()
    assert left >= server.MARGIN_X and right <= server.CANVAS_WIDTH - server.MARGIN_X
    assert top >= server.MARGIN_Y and bottom <= server.CANVAS_HEIGHT - server.MARGIN_Y


def gap_between_bars(img: Image.Image) -> int:
    """Height of the white band between the first two dark bars."""
    rows = (np.array(img.convert("L")) < 128).any(axis=1)
    ink = np.flatnonzero(rows)
    return int(np.diff(ink).max()) - 1


def two_bars(gap: int) -> Image.Image:
    img = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((50, 50, 350, 80), fill="black")
    draw.rectangle((50, 80 + gap, 350, 110 + gap), fill="black")
    return img


def test_gap_threshold_decides_whether_blocks_split():
    # Below the threshold the gap only scales with the block; above it the
    # strips are spread over the free height
    assert gap_between_bars(server.segment_and_repack(two_bars(8))) <= 8 * server.MAX_SCALE
    assert gap_between_bars(server.segment_and_repack(two_bars(40))) > 60


def test_transparent_png_is_not_read_as_black():
    img = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((20, 20, 280, 80), fill=(0, 0, 0, 255))
    out = server.segment_and_repack(img)
    assert out.getpixel((2, 2)) == (255, 255, 255)


def test_light_frame_is_not_content():
    img = Image.new("RGB", (733, 343), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 732, 342), outline=(212, 212, 212))
    draw.rectangle((300, 120, 430, 220), fill="black")
    out = server.segment_and_repack(img)
    left, top, right, bottom = out.convert("L").point(lambda v: 255 - v).getbbox()
    assert right - left > server.SAFE_WIDTH * 0.3  # block scaled up, not shrunk to fit the frame


def test_blank_image_is_rejected():
    with pytest.raises(ValueError):
        server.segment_and_repack(Image.new("RGB", (300, 100), "white"))


def test_cut_plan():
    assert server.cut_plan(3, 0, True) == [False, False, True]
    assert server.cut_plan(4, 2, False) == [False, True, False, False]
    assert server.cut_plan(3, 1, True) == [True, True, True]


def test_copies_are_not_cumulative():
    img = server.segment_and_repack(label_with_bars(2))
    one = server.build_instructions(img, [False])
    assert len(server.build_instructions(img, [False] * 3)) == 3 * len(one)


def label_with_qr(payload: str) -> Image.Image:
    qr = cv2.QRCodeEncoder.create().encode(payload)
    qr = Image.fromarray(qr).convert("RGB").resize((120, 120), Image.Resampling.NEAREST)
    img = Image.new("RGB", (733, 343), "white")
    img.paste(qr, (590, 110))
    draw = ImageDraw.Draw(img)
    for i, y in enumerate((40, 90, 250)):
        draw.rectangle((40, y, 300 + i * 120, y + 30), fill="black")
    return img


def test_qr_is_full_height_at_right_edge_clear_of_text_and_still_decodes():
    out = server.segment_and_repack(label_with_qr("spool-43"))
    qr_x = server.CANVAS_WIDTH - server.MARGIN_X - server.SAFE_HEIGHT
    ink = np.array(out.convert("L")) < 128

    qr_cols = np.flatnonzero(ink[:, qr_x:].any(axis=0)) + qr_x
    qr_rows = np.flatnonzero(ink[:, qr_x:].any(axis=1))
    assert qr_cols.max() == server.CANVAS_WIDTH - server.MARGIN_X - 1
    assert qr_rows.min() == server.MARGIN_Y and qr_rows.max() == server.CANVAS_HEIGHT - server.MARGIN_Y - 1

    text_right = np.flatnonzero(ink[:, :qr_x].any(axis=0)).max()
    assert qr_cols.min() - text_right - 1 >= server.QR_TEXT_GAP

    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(np.array(out.convert("L")))
    assert decoded == "spool-43"


# --- batch upload, zip, multi-page pdf, endpoints -------------------------------------

client = TestClient(server.app)


def label_sized(count: int, size=(733, 343)) -> Image.Image:
    """A 62 x 29 mm label at 300 DPI with `count` dark bars (0 = blank)."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    for i in range(count):
        y = 20 + i * 70
        draw.rectangle((40, y, 600, y + 40), fill="black")
    return img


def png_bytes(count: int = 2, size=(733, 343)) -> bytes:
    buf = io.BytesIO()
    label_sized(count, size).save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes(pages: int) -> bytes:
    buf = io.BytesIO()
    imgs = [label_sized(1 + i) for i in range(pages)]
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=300)
    return buf.getvalue()


def zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buf.getvalue()


def preview(*uploads):
    response = client.post("/preview", files=[("files", u) for u in uploads])
    assert response.status_code == 200
    return response.json()


def test_preview_accepts_mixed_files_and_every_pdf_page():
    body = preview(
        ("a.png", png_bytes()), ("b.jpg", png_bytes()), ("c.pdf", pdf_bytes(3))
    )
    assert [l["name"] for l in body["labels"]] == ["a.png", "b.jpg", "c.pdf (1/3)", "c.pdf (2/3)", "c.pdf (3/3)"]
    assert body["errors"] == []
    assert all(l["png"].startswith("data:image/png;base64,") for l in body["labels"])


def test_zip_is_unpacked_in_natural_order_and_clutter_is_skipped():
    archive = zip_bytes({
        "labels/label-10.png": png_bytes(),
        "labels/label-9.png": png_bytes(),
        "__MACOSX/labels/._label-9.png": b"junk",
        "labels/.hidden.png": b"junk",
        "labels/notes.txt": b"hello",
        "labels/inner.zip": b"PK",
    })
    body = preview(("pack.zip", archive))
    assert [l["name"] for l in body["labels"]] == ["label-9.png", "label-10.png"]
    assert body["errors"] == []


def test_zip_member_path_is_reduced_to_its_basename():
    body = preview(("pack.zip", zip_bytes({"../../etc/evil.png": png_bytes()})))
    assert [l["name"] for l in body["labels"]] == ["evil.png"]


def test_broken_files_are_reported_per_file_without_hiding_good_ones():
    body = preview(
        ("good.png", png_bytes()),
        ("bad.png", b"not an image"),
        ("blank.png", png_bytes(0)),
        ("notes.txt", b"hello"),
        ("broken.zip", b"not a zip"),
    )
    assert [l["name"] for l in body["labels"]] == ["good.png"]
    codes = {e["name"]: e["code"] for e in body["errors"]}
    assert codes == {
        "bad.png": "unreadable",
        "blank.png": "no_content",
        "notes.txt": "unsupported",
        "broken.zip": "zip_unreadable",
    }


def test_oversized_zip_member_is_rejected(monkeypatch):
    monkeypatch.setattr(server, "MAX_FILE_BYTES", 1000)
    body = preview(("pack.zip", zip_bytes({"big.png": png_bytes()})))
    assert body["labels"] == [] and body["errors"][0]["code"] == "too_large"


def test_label_limit_is_enforced_once(monkeypatch):
    monkeypatch.setattr(server, "MAX_LABELS", 2)
    body = preview(("a.png", png_bytes()), ("b.png", png_bytes()), ("c.png", png_bytes()), ("d.png", png_bytes()))
    assert len(body["labels"]) == 2
    assert [e["code"] for e in body["errors"]] == ["too_many"]


@pytest.fixture
def printer(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "PRINTER_IDENTIFIER", "file:///dev/null")  # exists, so the 503 guard passes
    monkeypatch.setattr(server, "send", lambda instructions, **_: sent.append(instructions))
    return sent


def print_job(uploads, **form):
    return client.post("/print", files=[("files", u) for u in uploads], data=form)


def test_print_sends_every_label_times_copies(printer):
    response = print_job([("a.png", png_bytes()), ("p.pdf", pdf_bytes(2))], copies="3")
    assert response.status_code == 200
    assert response.json()["labels"] == 3 and response.json()["printed"] == 9
    assert len(printer) == 3  # one send per label, copies batched inside it


def test_cut_plan_spans_files_not_each_file(printer):
    # 3 labels x 2 copies, cut every 4th, no cut at end: a flag only on the 4th
    calls = []
    real = server.build_instructions
    server.build_instructions = lambda img, flags: calls.append(list(flags)) or real(img, flags)
    try:
        print_job([("a.png", png_bytes()), ("b.png", png_bytes()), ("c.png", png_bytes())],
                  copies="2", cut_every="4", cut_at_end="false")
    finally:
        server.build_instructions = real
    assert calls == [[False, False], [False, True], [False, False]]


def test_print_refuses_everything_when_any_file_is_unusable(printer):
    response = print_job([("good.png", png_bytes()), ("bad.png", b"nope")])
    assert response.status_code == 400
    assert response.json()["errors"][0]["name"] == "bad.png"
    assert printer == []


def test_print_validates_copies_and_job_size(printer, monkeypatch):
    assert print_job([("a.png", png_bytes())], copies="0").status_code == 422
    assert print_job([("a.png", png_bytes())], copies="51").status_code == 422
    monkeypatch.setattr(server, "MAX_PRINTS", 5)
    assert print_job([("a.png", png_bytes())], copies="6").status_code == 400
    assert printer == []


def test_print_reports_how_many_labels_were_sent_before_a_printer_failure(printer, monkeypatch):
    def flaky(instructions, **_):
        if printer:
            raise OSError("device gone")
        printer.append(instructions)

    monkeypatch.setattr(server, "send", flaky)
    response = print_job([("a.png", png_bytes()), ("b.png", png_bytes())], copies="2")
    assert response.status_code == 503 and "after 2 labels" in response.json()["detail"]


def test_web_ui_is_english():
    import re
    page = client.get("/").text
    assert client.get("/").status_code == 200 and 'lang="en"' in page
    assert not re.search("[ąćęłńóśźż]", page)
    assert "Choose files" in page and "Select all" in page


def test_print_only_the_selected_labels_and_cut_plan_covers_just_them(printer):
    calls = []
    real = server.build_instructions
    server.build_instructions = lambda img, flags: calls.append(list(flags)) or real(img, flags)
    try:
        response = client.post(
            "/print",
            files=[("files", (n, png_bytes())) for n in ("a.png", "b.png", "c.png")],
            data={"copies": "2", "cut_every": "0", "cut_at_end": "true", "selected": ["2", "0"]},
        )
    finally:
        server.build_instructions = real
    assert response.status_code == 200
    assert response.json()["labels"] == 2 and response.json()["printed"] == 4
    assert len(printer) == 2                          # a.png and c.png, in job order
    assert calls == [[False, False], [False, True]]   # one cut, after the last selected copy


@pytest.mark.parametrize("selected", [["3"], ["-1"], ["0", "0"]])
def test_print_rejects_an_invalid_selection(printer, selected):
    response = client.post(
        "/print",
        files=[("files", ("a.png", png_bytes())), ("files", ("b.png", png_bytes()))],
        data={"selected": selected},
    )
    assert response.status_code == 400 and printer == []


def test_design_tokens_and_fonts_are_served():
    assert client.get("/web/tokens.css").status_code == 200
    assert client.get("/web/fonts/manrope-latin.woff2").status_code == 200
    assert "--accent-green: var(--green-500)" in client.get("/web/tokens.css").text


def test_sources_that_are_not_label_shaped_are_refused_instead_of_squeezed(monkeypatch):
    a4_png = png_bytes(1, size=(2480, 3508))
    photo = png_bytes(1, size=(1600, 1200))
    buf = io.BytesIO()
    imgs = [label_sized(1), label_sized(1, size=(2480, 3508))]   # label page, then an A4 page
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=300)

    # a refused PDF must not even be rendered
    monkeypatch.setattr(server, "convert_from_bytes", lambda *a, **k: pytest.fail("rendered"))
    body = preview(("a4.png", a4_png), ("photo.png", photo), ("mixed.pdf", buf.getvalue()),
                   ("pack.zip", zip_bytes({"a4.png": a4_png})))
    assert body["labels"] == []
    assert {e["name"]: e["code"] for e in body["errors"]} == {
        "a4.png": "wrong_format", "photo.png": "wrong_format", "mixed.pdf": "wrong_format"}
    assert [e["detail"] for e in body["errors"] if e["name"] == "mixed.pdf"] == ["210 × 297 mm"]


def test_label_proportions_are_accepted_in_either_orientation_and_close_sizes():
    assert server.fits_label(733, 343) and server.fits_label(343, 733)    # 62 x 29 mm, landscape and portrait
    assert server.fits_label(496, 232)                                    # same label at 203 DPI
    assert server.fits_label(100, 50) and server.fits_label(70, 30)       # close enough to be scaled safely
    assert not server.fits_label(210, 297) and not server.fits_label(1600, 1200)
    assert not server.fits_label(50, 30) and not server.fits_label(0, 10)


def test_print_refuses_a_wrong_format_file_and_prints_nothing(printer):
    response = print_job([("good.png", png_bytes()), ("a4.png", png_bytes(1, size=(2480, 3508)))])
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "wrong_format"
    assert printer == []


def test_print_progress_is_reported_per_chunk_and_cleaned_up(monkeypatch):
    seen, sends = [], []

    def spy_send(instructions, **_):
        seen.append(dict(server.progress["job-12345678"]))   # what the page would read right now
        sends.append(instructions)

    monkeypatch.setattr(server, "PRINTER_IDENTIFIER", "file:///dev/null")
    monkeypatch.setattr(server, "send", spy_send)
    response = client.post(
        "/print", files=[("files", ("a.png", png_bytes()))], data={"copies": "12", "job_id": "job-12345678"})
    assert response.status_code == 200
    assert len(sends) == 3                                    # 12 copies in chunks of 5, 5, 2
    assert seen == [
        {"stage": "printing", "done": 0, "total": 12},
        {"stage": "printing", "done": 5, "total": 12},
        {"stage": "printing", "done": 10, "total": 12},
    ]
    assert "job-12345678" not in server.progress              # gone once the request finished
    assert client.get("/progress/job-12345678").json() == {"stage": "unknown"}


def test_progress_entry_is_removed_even_when_the_job_fails(printer):
    response = client.post(
        "/print", files=[("files", ("bad.png", b"nope"))], data={"job_id": "job-87654321"})
    assert response.status_code == 400
    assert "job-87654321" not in server.progress


@pytest.mark.parametrize("job_id", ["short", "has space 12345", "x" * 65, "semi;colon-1234"])
def test_print_rejects_a_malformed_job_id(printer, job_id):
    response = client.post(
        "/print", files=[("files", ("a.png", png_bytes()))], data={"job_id": job_id})
    assert response.status_code == 422 and printer == []


def test_a_long_line_does_not_tear_the_text_block_apart():
    # One 520 px line caps the text scale by width, which leaves spare height; the gaps must stay modest
    img = Image.new("RGB", (733, 343), "white")
    draw = ImageDraw.Draw(img)
    for y, h, w in ((20, 24, 190), (64, 20, 110), (104, 34, 520), (158, 52, 140)):
        draw.rectangle((35, y, 35 + w, y + h), fill="black")
    qr = cv2.QRCodeEncoder.create().encode("https://example.org/inventory?spool=10")
    img.paste(Image.fromarray(qr).convert("RGB").resize((125, 125), Image.Resampling.NEAREST), (585, 110))

    out = server.segment_and_repack(img)
    qr_x = server.CANVAS_WIDTH - server.MARGIN_X - server.SAFE_HEIGHT
    rows = np.flatnonzero((np.array(out.convert("L"))[:, :qr_x] < 128).any(axis=1))
    runs = np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1)
    heights = [len(r) for r in runs]
    gaps = [int(runs[i + 1][0] - runs[i][-1] - 1) for i in range(len(runs) - 1)]
    assert len(runs) == 4
    assert max(gaps) <= sum(heights) / len(heights) + 1         # no gap taller than a typical block
    top, bottom = int(rows[0]), server.CANVAS_HEIGHT - 1 - int(rows[-1])
    assert abs(top - bottom) <= 2                               # the block is centred in the free height
    # Only the long line is held back by its width; the other lines keep filling the height
    ratios = [h / src for h, src in zip(heights, (25, 21, 35, 53))]
    assert ratios[2] < 0.85 * min(ratios[0], ratios[1], ratios[3])


# --- discovery -------------------------------------------------------------------------

def test_info_describes_the_service_and_reflects_the_printer_state(monkeypatch):
    monkeypatch.setattr(server, "PRINTER_IDENTIFIER", "file:///dev/null")
    info = client.get("/info").json()
    assert info["service"] == "label-printer" and info["version"] == server.API_VERSION
    assert info["printer"] == {"model": server.MODEL, "connected": True}
    assert info["label"] == {"id": "62x29", "width_mm": 62, "height_mm": 29, "dpi": 300}
    assert info["limits"]["max_copies"] == server.MAX_COPIES and info["limits"]["max_labels"] == server.MAX_LABELS
    assert {".pdf", ".png", ".jpg", ".zip"} <= set(info["accepts"])

    monkeypatch.setattr(server, "PRINTER_IDENTIFIER", "file:///nonexistent/lp0")
    assert client.get("/info").json()["printer"]["connected"] is False


def test_the_avahi_announcement_matches_what_the_server_reports():
    import xml.etree.ElementTree as ET
    from label_printer import announce
    from label_printer.config import Settings

    xml = announce.service_xml(Settings())
    service = ET.fromstring(xml[xml.index("<service-group>"):]).find("service")
    txt = dict(t.text.split("=", 1) for t in service.findall("txt-record"))
    info = client.get("/info").json()
    assert service.findtext("type") == "_labelprinter._tcp" and int(service.findtext("port")) == 8000
    assert txt["v"] == str(info["version"]) and txt["path"] == "/info"
    assert txt["model"] == info["printer"]["model"] and txt["label"] == info["label"]["id"]
    assert int(txt["dpi"]) == info["label"]["dpi"]
