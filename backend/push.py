"""Real notifications on phones and computers: standard Web Push (VAPID), so no app store and no third-party account.

Works in Chrome, Edge, Firefox and Safari 16.4+. On an iPhone or iPad the app must first be added to the Home Screen.
Without VAPID keys every call is a harmless no-op, so the app still runs on a laptop with nothing set up.
"""
import os, json, logging
from concurrent.futures import ThreadPoolExecutor
from pywebpush import webpush, WebPushException

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push")

def configured():
    return bool(os.getenv("VAPID_PUBLIC_KEY") and os.getenv("VAPID_PRIVATE_KEY"))

def public_key():
    return os.getenv("VAPID_PUBLIC_KEY", "")

def _send_one(sub, payload):
    """Returns 'ok', 'gone' (the device unsubscribed: forget it) or 'error' (try again next time)."""
    try:
        webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}, data=json.dumps(payload),
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"], vapid_claims={"sub": os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")}, ttl=86400, timeout=10)
        return "ok"
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410): return "gone"
        logging.warning("push failed (%s): %s", code, e)
    except Exception:
        logging.exception("push failed")
    return "error"

def deliver(subs, payload, forget):
    """Send to every device of one person. forget(endpoint) is called for devices that are gone."""
    for s in subs:
        if _send_one(s, payload) == "gone": forget(s["endpoint"])

def enqueue(subs, payload, forget):
    """Send in the background so a request or a sweep never waits on the network."""
    if not subs or not configured(): return
    _pool.submit(deliver, subs, payload, forget)
