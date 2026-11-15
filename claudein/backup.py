"""Back up and restore pairings (plan 4.6): the certificate, its key and the pairing list (hashed codes only), in one file.

The file lets someone pretend to be ClaudeIn to the phones, so it must be kept somewhere private.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import tls
from .config import write_atomic
from .pairing import DEVICES, _owner_only

KIND = "claudein-pairings-backup"


def export(folder: Path, dest: Path) -> None:
    data = {
        "kind": KIND,
        "version": 1,
        "cert": (folder / tls.CERT).read_text(encoding="utf-8"),
        "key": (folder / tls.KEY).read_text(encoding="utf-8"),
        "devices": json.loads((folder / DEVICES).read_text(encoding="utf-8")) if (folder / DEVICES).exists() else {},
    }
    write_atomic(dest, json.dumps(data, indent=2))
    _owner_only(dest)


def restore(folder: Path, src: Path) -> None:
    """Puts a backup back. ClaudeIn must be restarted afterwards to use the restored certificate."""
    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("kind") != KIND:
        raise ValueError("That file is not a ClaudeIn pairings backup.")
    cert, key, devices = data.get("cert"), data.get("key"), data.get("devices")
    if not isinstance(cert, str) or not isinstance(key, str) or not isinstance(devices, dict):
        raise ValueError("That backup is incomplete.")
    # check it is a real certificate and key before replacing anything
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    x509.load_pem_x509_certificate(cert.encode())
    serialization.load_pem_private_key(key.encode(), password=None)
    write_atomic(folder / tls.CERT, cert)
    write_atomic(folder / tls.KEY, key)
    _owner_only(folder / tls.KEY)
    write_atomic(folder / DEVICES, json.dumps(devices, indent=2))
