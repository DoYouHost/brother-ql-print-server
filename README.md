# brother-ql-print-server

Print server for Brother QL label printers, made for a Raspberry Pi. Send PDF, PNG, JPG or ZIP files over HTTP (or use the built-in web page) and it repacks each one to fill the label and prints it. Tested on a QL-600 with 62 × 29 mm labels (DK-11209).

## Install

Download the package for your system from [Releases](https://github.com/DoYouHost/brother-ql-print-server/releases) and install it. The service starts on its own, also after a reboot.

```sh
sudo apt install ./label-printer_<version>_arm64.deb    # Raspberry Pi OS / Debian 13 (amd64 too)
sudo dnf install ./label-printer-<version>-1.aarch64.rpm  # Fedora (x86_64 too; not tested on hardware)
```

Open `http://<pi>:8000/`. Settings live in `/etc/default/label-printer`:

| Variable | Default | Meaning |
|---|---|---|
| `PRINTER_MODEL` | `QL-600` | Any model `brother_ql` knows |
| `PRINTER_LABEL` | `62x29` | `62x29`, `54x29`, `52x29`, `102x51` (QL-1xxx only) |
| `PRINTER_IDENTIFIER` | `file:///dev/usb/lp0` | `tcp://host:9100` or `usb://0x04f9:0x20c0` also work |
| `LABEL_PRINTER_HOST` / `_PORT` | `0.0.0.0` / `8000` | Listen address |

Only the QL-600 over USB is tested on hardware. A wrong value stops the service with a message saying what to change (`python -m label_printer check`).

## Discovery (mDNS)

While the server runs, Avahi announces it as DNS-SD service `_labelprinter._tcp` on the configured port.

| TXT key | Example | |
|---|---|---|
| `v` | `1` | API version, same as `version` in `/info` |
| `path` | `/info` | Where to read identity and capabilities |
| `model` | `QL-600` | |
| `label` | `62x29` | |
| `dpi` | `300` | |

After resolving, call `GET /info` and check `printer.connected`. Notes for clients:
- Prefer the resolved IP over `rpi-label-printer.local`; Android does not resolve `.local` names reliably.
- mDNS does not cross VLANs or guest Wi-Fi isolation, so offer a manual address field.
- There is no authentication: anyone on the LAN can print.

## API

### `GET /info`

```json
{"service": "label-printer", "version": 1,
 "printer": {"model": "QL-600", "connected": true},
 "label": {"id": "62x29", "width_mm": 62, "height_mm": 29, "dpi": 300},
 "limits": {"max_copies": 50, "max_labels": 100, "max_prints": 500, "max_file_mb": 25},
 "accepts": [".png", ".jpg", ".jpeg", ".pdf", ".zip"]}
```

`printer.connected` is `null` for network and libusb printers, which cannot be probed. `version` changes only on breaking API changes.

### Accepted input

One job takes any number of files of mixed type, sent as repeated `files` fields.

- `.pdf`: every page is a label. `.png`, `.jpg`/`.jpeg`: one label each.
- `.zip`: unpacked in memory; members are sorted naturally, hidden files, `__MACOSX`, nested zips and non-label files are skipped.
- The source must have the label's proportions (62 : 29, either orientation, within 15%). Anything else (an A4 page, a photo) is refused with `wrong_format` and the size found, never cropped or squeezed.
- Limits: 100 labels per job, 500 labels × copies, 25 MB per file or zip member, 100 MB unpacked per zip, 200 zip members, 20 MP per image or PDF page.
- The server finds the QR code, redraws it at full label height, flush right, and repacks the rest of the content left of it. Use a sharp source (300 DPI).

### `POST /preview`

`multipart/form-data`, field `files` (repeatable). Always `200`:

```json
{"labels": [{"name": "set1.pdf p2", "png": "data:image/png;base64,..."}],
 "errors": [{"name": "photo.jpg", "code": "wrong_format", "detail": "4000 × 3000 px"}]}
```

`png` is the exact 696 × 271 px layout that would be printed. The order of `labels` is what `selected` in `/print` refers to.

### `POST /print`

`multipart/form-data`:

| Field | Default | |
|---|---|---|
| `files` | required | As above |
| `copies` | `1` | Copies of each label, 1 to 50 |
| `cut_at_end` | `true` | Cut after the last label |
| `cut_every` | `0` | Cut after every N-th label of the job (`0` off, `1` every label) |
| `selected` | all | Repeatable; indices into the `/preview` order, only these print |
| `job_id` | none | 8 to 64 chars of `A-Za-z0-9_-`, chosen by the client, for progress |

Success: `200` `{"status": "ok", "labels": 3, "copies": 2, "printed": 6, "cut_at_end": true, "cut_every": 0}`.

One job prints at a time. If any file is unusable, nothing is printed.

### `GET /progress/{job_id}`

Poll while `POST /print` with that `job_id` is running (it blocks until the job is done):

- `{"stage": "processing"}`
- `{"stage": "printing", "done": 4, "total": 6}` (labels × copies sent so far)
- `{"stage": "unknown"}` for an unknown or finished id

State is in memory and lives only as long as the request.

## Errors

| Status | When | Body |
|---|---|---|
| `400` | A file or page is unusable, no labels, bad `selected`, or labels × copies over 500 | `{"detail": "...", "errors": [{name, code, detail}]}` (`errors` is empty for the last two) |
| `422` | Invalid form field (e.g. `copies=0`, malformed `job_id`, no `files`) | FastAPI validation body |
| `503` | Printer not connected, or it failed mid-job | `{"detail": "..."}`; for a mid-job failure the text says how many labels were sent |

Error `code` values (same in `/preview` `errors` and `/print` `400`):

| Code | Meaning |
|---|---|
| `wrong_format` | Proportions do not match the label; `detail` has the size |
| `too_large` | File, zip member or image over the limits |
| `too_many` | More than 100 labels in one job |
| `no_content` | Nothing to print found in the image |
| `unreadable` | Corrupt or undecodable file or PDF page |
| `unsupported` | Not a PDF, PNG, JPG or ZIP |
| `zip_unreadable`, `zip_limits` | Broken zip, or too many or too large members |
| `password` | Password-protected file |

Handle unknown codes by showing `detail`.

## Development

```sh
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt pytest httpx
python -m label_printer     # serves on 0.0.0.0:8000
pytest
```

Packages are built by `deploy/build-deb.sh` and `deploy/build-rpm.sh`; pushing a tag `v<version>` builds and publishes them through GitHub Actions. Details for contributors and AI agents are in [AGENTS.md](AGENTS.md).

## License

[AGPL-3.0-or-later](LICENSE)
