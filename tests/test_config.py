"""Settings, the label-driven canvas and the command line."""

import subprocess
import sys
import textwrap

import pytest

from label_printer import __main__ as cli, announce
from label_printer.config import ConfigError, Settings, load_settings, supported_labels


def test_defaults_describe_the_ql_600_with_dk_11209_labels_over_usb():
    s = load_settings({})
    assert (s.model, s.label, s.identifier, s.port) == ("QL-600", "62x29", "file:///dev/usb/lp0", 8000)
    assert s.backend == "linux_kernel"


def test_the_printer_can_be_given_as_a_device_a_network_address_or_a_usb_id():
    assert load_settings({"PRINTER_DEVICE": "/dev/usb/lp1"}).identifier == "file:///dev/usb/lp1"   # older name
    assert load_settings({"PRINTER_IDENTIFIER": "file:///dev/usb/lp2", "PRINTER_DEVICE": "/x"}).identifier == "file:///dev/usb/lp2"
    net = load_settings({"PRINTER_MODEL": "QL-720NW", "PRINTER_IDENTIFIER": "tcp://192.168.1.50:9100"})
    assert net.backend == "network"
    assert load_settings({"PRINTER_IDENTIFIER": "usb://0x04f9:0x20c0"}).backend == "pyusb"


@pytest.mark.parametrize("env, fragment", [
    ({"PRINTER_MODEL": "QL-9000"}, "PRINTER_MODEL 'QL-9000' is unknown"),
    ({"PRINTER_LABEL": "12"}, "PRINTER_LABEL '12' is not supported"),
    ({"PRINTER_LABEL": "29x90"}, "labels the layout can fill"),            # portrait: the layout cannot fill it yet
    ({"PRINTER_LABEL": "102x51"}, "cannot be printed on QL-600"),          # wide-format labels need a QL-1xxx
    ({"PRINTER_IDENTIFIER": "/dev/usb/lp0"}, "must start with file://"),
    ({"PRINTER_IDENTIFIER": "http://printer"}, "must start with file://"),
    ({"LABEL_PRINTER_PORT": "eighty"}, "must be a number between 1 and 65535"),
    ({"LABEL_PRINTER_PORT": "70000"}, "must be a number between 1 and 65535"),
])
def test_a_wrong_setting_is_refused_with_a_message_that_says_what_to_change(env, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_settings(env)


def test_only_landscape_die_cut_labels_are_offered_and_wide_ones_need_a_wide_printer():
    assert {"62x29", "54x29", "52x29"} <= set(supported_labels())
    assert "102x51" in supported_labels() and "102x51" not in supported_labels("QL-600")
    assert "102x51" in supported_labels("QL-1100") and "29x90" not in supported_labels()
    assert load_settings({"PRINTER_MODEL": "QL-1100", "PRINTER_LABEL": "102x51"}).label == "102x51"


def run_with_label(label: str) -> list:
    code = textwrap.dedent("""
        from PIL import Image, ImageDraw
        from label_printer import server
        img = Image.new("RGB", (733, 343), "white")
        ImageDraw.Draw(img).rectangle((60, 60, 500, 120), fill="black")
        out = server.segment_and_repack(img)
        print(server.CANVAS_WIDTH, server.CANVAS_HEIGHT, out.size[0], out.size[1],
              server.LABEL_MM[0], server.LABEL_MM[1], server.fits_label(733, 343))
    """)
    env = {"PRINTER_LABEL": label, "PATH": "/usr/bin"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_the_canvas_and_the_format_check_follow_the_configured_label():
    # 62x29 is the default; the others change the canvas and the accepted proportions from configuration alone
    assert run_with_label("62x29") == ["696", "271", "696", "271", "62", "29", "True"]
    assert run_with_label("54x29") == ["598", "271", "598", "271", "54", "29", "True"]    # a 62x29 source is within 15%
    assert run_with_label("52x29") == ["578", "271", "578", "271", "52", "29", "False"]   # but not for 52x29: refused


def test_announcement_is_generated_from_the_settings(tmp_path):
    target = tmp_path / "avahi" / "label-printer.service"
    announce.install(Settings(model="QL-720NW", label="54x29", port=9000), target)
    xml = target.read_text()
    assert "<port>9000</port>" in xml and "model=QL-720NW" in xml and "label=54x29" in xml
    assert "<type>_labelprinter._tcp</type>" in xml and oct(target.stat().st_mode)[-3:] == "644"
    announce.remove(target)
    assert not target.exists()
    announce.remove(target)    # removing twice is fine: the unit runs it on every stop


def test_command_line_reports_a_bad_setting_and_exits_with_2(monkeypatch, capsys):
    monkeypatch.setenv("PRINTER_LABEL", "29x90")
    assert cli.main(["check"]) == 2
    assert "labels the layout can fill" in capsys.readouterr().err


def test_command_line_check_describes_the_setup(monkeypatch, capsys):
    monkeypatch.setenv("PRINTER_IDENTIFIER", "file:///nonexistent/lp0")
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "model QL-600, label 62x29" in out and "printer not found" in out
