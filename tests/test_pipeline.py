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

import server


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


def png_bytes(count: int = 2) -> bytes:
    buf = io.BytesIO()
    label_with_bars(count).save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes(pages: int) -> bytes:
    buf = io.BytesIO()
    imgs = [label_with_bars(1 + i) for i in range(pages)]
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
    monkeypatch.setattr(server, "PRINTER_DEVICE", "/dev/null")  # exists, so the 503 guard passes
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


def test_web_ui_is_served_in_polish():
    response = client.get("/")
    assert response.status_code == 200 and 'lang="pl"' in response.text
    assert "Cut between" not in response.text and "Cut at end" not in response.text
