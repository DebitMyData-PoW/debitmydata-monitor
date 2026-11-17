"""Web push: a notification for one device, encrypted for it and signed by the monitor, sent to
its browser's push service (the browser shows it, even when the stats page is closed).

  the encryption   RFC 8291 and RFC 8188 (aes128gcm, one record): only that device can read it
  the signature    VAPID, RFC 8292: the push service knows it comes from the monitor's key

The monitor's key (P-256) is a file only the chain watch's user can read (DMDW_PUSH_KEY_FILE,
/var/lib/debitmydata-monitor/push/vapid.pem, made once by install.sh: dmd-chainwatch
push-keys). Its public half is in the page, through /push: the browser ties each device's
subscription to it. A new key means every device must turn notifications on again, so the key
file goes in the backups.

Notices only go to the browsers' push services (PUSH_HOSTS): a subscription that names another
address is refused, so nobody can make the monitor send requests elsewhere.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
TEST_HOSTS: set[str] = set()  # the tests' stand-in push service (http allowed)
RECORD_SIZE = 4096
USER_AGENT = "DebitMyData-Monitor"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def host_allowed(endpoint: str) -> bool:
    """A browser push service's address (https), or the tests' stand-in."""
    try:
        url = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return False
    host = (url.hostname or "").lower()
    if host in TEST_HOSTS:
        return url.scheme in ("http", "https")
    if url.scheme != "https" or url.username or url.password:
        return False
    return any(host == known or host.endswith("." + known) for known in PUSH_HOSTS)


@dataclass(frozen=True)
class Keys:
    private: ec.EllipticCurvePrivateKey

    @property
    def public(self) -> str:
        """The public key as the browser takes it (applicationServerKey): 65 bytes, base64url."""
        return b64u(
            self.private.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )


def load_keys(path: Path) -> Keys | None:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), None)
    except (OSError, ValueError):
        return None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        return None
    return Keys(key)


def make_keys(path: Path) -> tuple[Keys, bool]:
    """The key in that file, made first if there is none. Returns it, and whether it is new."""
    existing = load_keys(path)
    if existing:
        return existing, False
    if path.exists():
        raise ValueError(f"{path} is not a P-256 key: move it away to make a new one")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return Keys(key), True


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(hashes.SHA256(), length, salt, info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """The notice, encrypted for the device whose keys these are (aes128gcm, one record)."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) < 16:
        raise ValueError("not a device's push keys")
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("the notice is too long")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    local = ec.generate_private_key(ec.SECP256R1())
    as_public = local.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = local.exchange(ec.ECDH(), peer)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + cipher


def vapid(keys: Keys, endpoint: str, subject: str, now: float | None = None) -> str:
    """The Authorization header for the push service at that address (RFC 8292)."""
    url = urllib.parse.urlsplit(endpoint)
    claims = {
        "aud": f"{url.scheme}://{url.netloc}",
        "exp": int((now or time.time()) + 12 * 3600),
        "sub": subject if subject.startswith(("mailto:", "https:")) else f"mailto:{subject}",
    }
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signature = keys.private.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={head}.{body}.{b64u(raw)}, k={keys.public}"


def deliver(
    keys: Keys,
    subject: str,
    device: dict[str, Any],
    message: dict[str, Any],
    timeout: float = 10,
) -> tuple[int, str]:
    """Sends one notice. Returns the push service's HTTP code (0: not sent) and its answer."""
    endpoint = str(device["endpoint"])
    if not host_allowed(endpoint):
        return 0, "not a push service's address"
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    try:
        body = encrypt(payload, str(device["p256dh"]), str(device["auth"]))
    except ValueError as exc:
        return 0, str(exc)
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "TTL": "86400",
        "Urgency": "high",  # a phone saving its battery shows it at once
        "Authorization": vapid(keys, endpoint, subject),
        "User-Agent": USER_AGENT,
    }
    topic = "".join(ch for ch in str(message.get("tag") or "") if ch.isalnum() or ch in "-_")
    if topic:
        headers["Topic"] = topic[:32]  # a newer notice of the same kind replaces one not delivered
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(200).decode("utf-8", "replace")
        except OSError:
            text = ""
        return exc.code, text
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, str(exc)[:200]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push service that answers with a redirect is not followed elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None
