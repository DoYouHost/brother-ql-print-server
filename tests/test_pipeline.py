"""Pipeline and cut-plan checks; run with synthetic images, no printer needed."""

import numpy as np
import pytest
from PIL import Image, ImageDraw

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


def test_blank_image_is_rejected():
    with pytest.raises(ValueError):
        server.segment_and_repack(Image.new("RGB", (300, 100), "white"))


def test_cut_plan():
    assert server.cut_plan(3, 0, True) == [False, False, True]
    assert server.cut_plan(4, 2, False) == [False, True, False, False]
    assert server.cut_plan(3, 1, True) == [True, True, True]


def test_copies_are_not_cumulative():
    img = server.segment_and_repack(label_with_bars(2))
    one = server.build_instructions(img, 1, 0, False)
    assert len(server.build_instructions(img, 3, 0, False)) == 3 * len(one)
