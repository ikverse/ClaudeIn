"""The self-made HTTPS certificate the phones pin (plan 4.3): EC P-256, 10 years, made on first start."""

from __future__ import annotations

import datetime as dt
import hashlib
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT = "cert.pem"
KEY = "key.pem"


def ensure_certificate(folder: Path) -> None:
    """Makes cert.pem and key.pem in [folder] unless both are already there."""
    cert_path, key_path = folder / CERT, folder / KEY
    if cert_path.exists() and key_path.exists():
        return
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ClaudeIn")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def fingerprint(folder: Path) -> str:
    """SHA-256 of the certificate (DER), upper-case hex: exactly what the phone pins."""
    cert = x509.load_pem_x509_certificate((folder / CERT).read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()


def short_fingerprint(folder: Path) -> str:
    """The last 6 characters, for the owner to compare with what the phone shows."""
    return fingerprint(folder)[-6:]


def server_context(folder: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(folder / CERT, folder / KEY)
    return ctx
