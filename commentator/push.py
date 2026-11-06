"""Web push: the stats page's alerts on a phone's home screen (docs/tracking.md#alerts).

A browser that turned alerts on is subscribed to a push service (Google's, Apple's, Mozilla's,
Microsoft's) and its subscription is kept in the chat table by the site stack's chat function. To alert
it, the booth encrypts a small JSON message to the browser's key (RFC 8291, aes128gcm) and posts it to
the subscription's endpoint, signed with the booth's own key (VAPID, RFC 8292). That key is made the
first time and kept in log_dir; its public half goes in the chat table, where the page reads it to
subscribe.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

KEY_FILE = "web-push-key.pem"   # under log_dir: the booth's signing key (never in a problem report)
TOPICS = ("live", "liveleague", "round", "record", "recap")
RECORD_SIZE = 4096
CONTACT = "https://github.com/jjmerri/gspro-shotcast"   # VAPID's "sub": who a push service can reach


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _point(public) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return public.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, as_private=None, salt: bytes | None = None) -> bytes:
    """`payload` encrypted for one browser (RFC 8291): the aes128gcm body, header included.
    `as_private` and `salt` are fresh each message; given only for the RFC's test vector."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(as_private.public_key())
    salt = salt or os.urandom(16)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + body


def load_key(log_dir: str | Path):
    """The booth's signing key, made (and kept in log_dir) the first time."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    path = Path(log_dir) / KEY_FILE
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(pem)
    os.replace(tmp, path)
    return key


def public_key(key) -> str:
    """What the page subscribes with (applicationServerKey)."""
    return b64u(_point(key.public_key()))


def vapid(key, endpoint: str, contact: str = CONTACT, now: float | None = None) -> str:
    """The Authorization header for one push service: a signed token (ES256 JWT) and our public key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": contact}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={public_key(key)}"


@dataclass
class Subscription:
    id: str          # its sort key in the table
    endpoint: str
    p256dh: str
    auth: str
    topics: frozenset[str]


@dataclass
class Alert:
    topic: str       # one of TOPICS
    title: str
    body: str
    url: str         # the page's link (a "#view?..." hash), opened when the alert is tapped
    tag: str | None = None   # a newer alert with the same tag replaces the older on the phone
    ttl: int = 86400         # seconds a push service holds it for a phone that's off

    def payload(self) -> bytes:
        return json.dumps({"title": self.title, "body": self.body, "url": self.url, "tag": self.tag},
                          separators=(",", ":")).encode()


def send(alert: Alert, subs: list[Subscription], key, client=None, contact: str = CONTACT) -> tuple[int, list[str]]:
    """Push `alert` to the subscriptions that asked for its topic. Returns how many it reached and
    the ids of the subscriptions that are gone (the push service said 404 or 410), to be dropped."""
    import httpx

    data = alert.payload()
    sent, gone = 0, []
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        for sub in subs:
            if alert.topic not in sub.topics:
                continue
            try:
                r = client.post(sub.endpoint, content=encrypt(data, sub.p256dh, sub.auth), headers={
                    "Authorization": vapid(key, sub.endpoint, contact), "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream", "TTL": str(alert.ttl), "Urgency": "normal"})
            except Exception as e:   # one phone's push service down isn't everyone's
                log.info("Alerts: couldn't reach %s: %s", urlsplit(sub.endpoint).netloc, e)
                continue
            if r.status_code in (404, 410):
                gone.append(sub.id)
            elif r.status_code >= 400:
                log.info("Alerts: %s answered %s: %s", urlsplit(sub.endpoint).netloc, r.status_code, r.text[:200])
            else:
                sent += 1
    finally:
        if own:
            client.close()
    return sent, gone
