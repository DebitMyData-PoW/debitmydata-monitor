"""The notices are encrypted for the device and signed with the monitor's key: a stand-in for the
browser's side (decrypt, check the signature) shows both."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from chainwatch import push


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """A browser's keys for one subscription, and how it reads a notice."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )

    def subscription(self, endpoint: str) -> dict[str, Any]:
        return {
            "endpoint": endpoint,
            "p256dh": push.b64u(self.public),
            "auth": push.b64u(self.auth),
        }

    def read(self, body: bytes) -> Any:
        salt, size, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        assert size == 4096
        sender = body[21 : 21 + idlen]
        shared = self.key.exchange(
            ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender)
        )

        def hkdf(salt_: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
            return HKDF(hashes.SHA256(), n, salt_, info).derive(ikm)

        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.public + sender, 32)
        cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


def check_vapid(header: str, endpoint_origin: str) -> dict[str, Any]:
    token = header.split("t=")[1].split(",")[0]
    key = header.split("k=")[1].strip()
    head, body, sig = token.split(".")
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(key))
    raw = unb64(sig)
    public.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{body}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    claims: dict[str, Any] = json.loads(unb64(body))
    assert claims["aud"] == endpoint_origin
    return claims


def test_keys_are_made_once(tmp_path: Path) -> None:
    path = tmp_path / "push" / "vapid.pem"
    keys, new = push.make_keys(path)
    assert new and path.stat().st_mode & 0o777 == 0o600
    again, new = push.make_keys(path)
    assert not new and again.public == keys.public
    assert len(unb64(keys.public)) == 65
    path.write_text("not a key")
    with pytest.raises(ValueError):
        push.make_keys(path)


def test_only_the_push_services(monkeypatch: pytest.MonkeyPatch) -> None:
    assert push.host_allowed("https://fcm.googleapis.com/fcm/send/abc")
    assert push.host_allowed("https://web.push.apple.com/QGx")
    assert push.host_allowed("https://wns2-par02p.notify.windows.com/w/?token=x")
    assert not push.host_allowed("http://fcm.googleapis.com/fcm/send/abc")
    assert not push.host_allowed("https://fcm.googleapis.com.evil.example/x")
    assert not push.host_allowed("https://user:pw@fcm.googleapis.com/x")
    assert not push.host_allowed("https://169.254.169.254/latest")
    monkeypatch.setattr(push, "TEST_HOSTS", {"127.0.0.1"})
    assert push.host_allowed("http://127.0.0.1:9/x")


def test_a_notice_reaches_the_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys, _ = push.make_keys(tmp_path / "vapid.pem")
    device = Device()
    arrived: list[dict[str, Any]] = []

    class Service(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            arrived.append(
                {
                    "headers": dict(self.headers),
                    "claims": check_vapid(self.headers["Authorization"], origin),
                    "message": device.read(body),
                }
            )
            self.send_response(410 if "gone" in self.path else 201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Service)
    origin = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(push, "TEST_HOSTS", {"127.0.0.1"})
    try:
        message = {"title": "New block", "body": "Block 149,201 · ✓", "url": "/", "tag": "block"}
        code, _ = push.deliver(
            keys, "https://stats.debitmydata.io", device.subscription(origin + "/d1"), message
        )
        assert code == 201
        got = arrived[0]
        assert got["message"] == message
        assert got["claims"]["sub"] == "https://stats.debitmydata.io"
        assert got["headers"]["Content-Encoding"] == "aes128gcm"
        assert got["headers"]["Topic"] == "block"
        code, _ = push.deliver(
            keys, "ops@example.com", device.subscription(origin + "/gone"), message
        )
        assert code == 410
        assert arrived[1]["claims"]["sub"] == "mailto:ops@example.com"
        # not a push service: nothing is sent
        monkeypatch.setattr(push, "TEST_HOSTS", set())
        code, why = push.deliver(keys, "x", device.subscription(origin + "/d1"), message)
        assert code == 0 and "push service" in why and len(arrived) == 2
    finally:
        server.shutdown()
