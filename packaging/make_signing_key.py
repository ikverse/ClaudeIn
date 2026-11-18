"""Makes the key that signs ClaudeIn's updates. Run once:  .venv\\Scripts\\python.exe packaging\\make_signing_key.py

- The private key goes to %USERPROFILE%\\.claudein-release\\signing-key.pem, outside the project, never in git. Keep a
  backup somewhere private (a USB stick you keep). Whoever has it can sign updates every ClaudeIn will install; if it is
  lost, ClaudeIn copies already out there won't accept new updates and need one manual install.
- The public key is written into claudein\\update_key.py, which ClaudeIn uses to check every update.
- An existing key is never replaced.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
KEY = Path.home() / ".claudein-release" / "signing-key.pem"
KEY_FILE = ROOT / "claudein" / "update_key.py"


def load_private_key(path: Path = KEY) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]


def public_key_text(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def key_file_text(public: str) -> str:
    return ('"""The public half of the key that signs ClaudeIn\'s updates (made by packaging/make_signing_key.py).\n\n'
            "ClaudeIn installs an update only if its signature matches this key. The private half stays on the owner's PC.\n"
            '"""\n\n'
            f'PUBLIC_KEY = "{public}"\n')


def main() -> None:
    if KEY.exists():
        print("A signing key already exists, so nothing was changed:", KEY)
        public = public_key_text(load_private_key())
    else:
        KEY.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        print("Signing key made:", KEY)
        print("Keep a private backup of it. Never put it in the project or on GitHub.")
        public = public_key_text(key)
    KEY_FILE.write_text(key_file_text(public), encoding="utf-8")
    print("Public key written to", KEY_FILE, ":", public)


if __name__ == "__main__":
    sys.exit(main())
