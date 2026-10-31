"""Run once: python gen_vapid.py  — prints the three lines to paste into your .env (keep the private key secret)."""
import base64
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid

v = Vapid(); v.generate_keys()
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
priv = v.private_key.private_numbers().private_value.to_bytes(32, "big")
print(f"VAPID_PUBLIC_KEY={b64(pub)}\nVAPID_PRIVATE_KEY={b64(priv)}\nVAPID_SUBJECT=mailto:you@example.com")
