"""The push layer against a fake browser push service: keys from gen_vapid.py must sign, encrypt and send."""
import base64, os, subprocess, sys, pathlib, json
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import push

ROOT = pathlib.Path(__file__).resolve().parents[1]
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()

@pytest.fixture
def keys(monkeypatch):
    out = subprocess.run([sys.executable, str(ROOT / "gen_vapid.py")], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        k, v = line.split("=", 1); monkeypatch.setenv(k, v)

def browser_sub(endpoint="https://fcm.googleapis.com/fcm/send/abc"):
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "p256dh": b64(pub), "auth": b64(os.urandom(16))}

def fake_post(monkeypatch, status):
    """Stand in for the browser's push service: record the request, answer with a real Response."""
    import requests
    seen = []
    def post(url, data=None, headers=None, **kw):
        seen.append((url, headers, data)); r = requests.Response(); r.status_code = status; r._content = b""; r.reason = "stub"; return r
    monkeypatch.setattr(requests, "post", post)
    return seen

def test_unconfigured_is_a_noop(monkeypatch):
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False); monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    assert not push.configured(); push.enqueue([browser_sub()], {"title": "x"}, lambda e: None)

def test_a_real_signed_encrypted_push_is_sent(keys, monkeypatch):
    seen = fake_post(monkeypatch, 201)
    assert push.configured() and push._send_one(browser_sub(), {"title": "Hi", "url": "/?go=Alerts"}) == "ok"
    url, headers, body = seen[0]
    assert url.startswith("https://fcm.googleapis.com/") and headers["Authorization"].startswith("vapid ") and headers["Content-Encoding"] == "aes128gcm" and b"Hi" not in body   # encrypted

def test_gone_devices_are_forgotten(keys, monkeypatch):
    fake_post(monkeypatch, 410); forgotten = []
    push.deliver([browser_sub("https://fcm.googleapis.com/fcm/send/dead")], {"title": "x"}, forgotten.append)
    assert forgotten == ["https://fcm.googleapis.com/fcm/send/dead"]
    fake_post(monkeypatch, 500); push.deliver([browser_sub()], {"title": "x"}, forgotten.append); assert len(forgotten) == 1         # a hiccup keeps the device
