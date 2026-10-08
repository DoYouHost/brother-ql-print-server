"""Settings, read from the environment.

Installed from the package they live in /etc/default/label-printer, which
systemd loads before starting the service.
"""

import os
from dataclasses import dataclass
from typing import List, Mapping, Optional

from brother_ql.labels import ALL_LABELS, FormFactor
from brother_ql.models import ALL_MODELS

LABEL_DPI = 300  # every QL model prints 300 dpi
DEFAULT_MODEL = "QL-600"
DEFAULT_LABEL = "62x29"
DEFAULT_IDENTIFIER = "file:///dev/usb/lp0"
# Where the printer is, by URL scheme -> the brother_ql backend that talks to it
BACKENDS = {"file": "linux_kernel", "tcp": "network", "usb": "pyusb"}


class ConfigError(Exception):
    """A setting is wrong; the message says what to change."""


def find_label(label_id: str):
    return next((label for label in ALL_LABELS if label.identifier == label_id), None)


def layout_supported(label) -> bool:
    """The layout engine places text left of a full-height QR code, which needs a landscape die-cut label."""
    return label.form_factor == FormFactor.DIE_CUT and label.dots_printable[0] > label.dots_printable[1]


def printable_on(label, model: str) -> bool:
    # Checked here because Label.works_with_model() in brother_ql_inventree tests the wrong list
    return not label.restricted_to_models or model in label.restricted_to_models


def supported_labels(model: Optional[str] = None) -> List[str]:
    return [
        label.identifier for label in ALL_LABELS
        if layout_supported(label) and (model is None or printable_on(label, model))
    ]


def device_path(identifier: str) -> Optional[str]:
    """The device node of a file:// printer; None for network and usb ones, which cannot be probed cheaply."""
    return identifier[len("file://"):] if identifier.startswith("file://") else None


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    label: str = DEFAULT_LABEL
    identifier: str = DEFAULT_IDENTIFIER
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def backend(self) -> str:
        return BACKENDS[self.identifier.split("://", 1)[0]]



def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    model = env.get("PRINTER_MODEL", DEFAULT_MODEL)
    label_id = env.get("PRINTER_LABEL", DEFAULT_LABEL)
    identifier = env.get("PRINTER_IDENTIFIER") or (
        f"file://{env['PRINTER_DEVICE']}" if env.get("PRINTER_DEVICE") else DEFAULT_IDENTIFIER  # older name
    )

    if model not in [m.identifier for m in ALL_MODELS]:
        raise ConfigError(
            f"PRINTER_MODEL '{model}' is unknown; choose one of: {', '.join(m.identifier for m in ALL_MODELS)}")
    label = find_label(label_id)
    if label is None or not layout_supported(label):
        raise ConfigError(
            f"PRINTER_LABEL '{label_id}' is not supported; labels the layout can fill: {', '.join(supported_labels())}")
    if not printable_on(label, model):
        raise ConfigError(
            f"label {label_id} cannot be printed on {model}; labels for {model}: {', '.join(supported_labels(model))}")
    if identifier.split("://", 1)[0] not in BACKENDS or "://" not in identifier:
        raise ConfigError(
            f"PRINTER_IDENTIFIER '{identifier}' must start with file://, tcp:// or usb:// "
            f"(for example {DEFAULT_IDENTIFIER})")
    try:
        port = int(env.get("LABEL_PRINTER_PORT", "8000"))
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        raise ConfigError("LABEL_PRINTER_PORT must be a number between 1 and 65535") from None
    return Settings(model, label_id, identifier, env.get("LABEL_PRINTER_HOST", "0.0.0.0"), port)
