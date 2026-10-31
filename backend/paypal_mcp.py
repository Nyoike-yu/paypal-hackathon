"""PayPal's official MCP server (@paypal/mcp), used for one read-only job: looking up the status of an order.

Penny never sees this server's tools. The app exposes its own tool (lookup_topup), checks in the database that the order
belongs to the person asking, and only then calls get_order() here. The server is started with `--tools=orders.get`,
so even this process can do nothing but read an order.

It is optional and fails soft. With no PayPal keys, no Node.js (npx), no `mcp` package, or PAYPAL_MCP=off, start() does nothing
and callers use the PayPal REST API (or demo mode) instead. get_order() raises paypal.PayPalError on any problem, never anything else.
"""
import os, json, time, shlex, shutil, asyncio, logging, threading
import paypal

TOOL = "get_order"  # the one tool of the PayPal MCP server we call; it takes {"id": order_id}

_loop = None
_thread = None
_ready = threading.Event()
_session = None
_stop_ev = None
_restart_ev = None
_last_poke = 0.0

def enabled():
    return paypal.configured() and os.getenv("PAYPAL_MCP", "on").lower() != "off"

def available():
    """True while a live session with the MCP server is up."""
    return _ready.is_set()

def _split(text):
    """Split a command line, keeping "quoted paths with spaces" together (C:\\My Projects\\python.exe on Windows)."""
    return [a.strip('"') for a in shlex.split(text, posix=(os.name != "nt"))]

def _command():
    # PAYPAL_MCP_COMMAND exists so tests can point at a stand-in server; leave it unset in real use.
    custom = _split(os.getenv("PAYPAL_MCP_COMMAND", ""))
    if custom: return custom[0], custom[1:]
    return "npx", ["-y", os.getenv("PAYPAL_MCP_PACKAGE", "@paypal/mcp@1.8.1"), "--tools=orders.get"]

# Network and npm settings the child needs to download and run the package (behind a proxy or a custom CA, npx hangs without them).
_NETWORK_VARS = {"http_proxy", "https_proxy", "no_proxy", "all_proxy", "node_extra_ca_certs", "ssl_cert_file", "ssl_cert_dir",
                 "npm_config_registry", "npm_config_cache", "npm_config_userconfig", "npm_config_proxy", "npm_config_https_proxy", "npm_config_cafile"}

def _child_env(token):
    """What the MCP server process gets: network settings plus the PayPal access token and nothing else.
    It is third-party code, so it never sees our Anthropic key, database URL, signing key or PayPal secret."""
    env = {k: v for k, v in os.environ.items() if k.lower() in _NETWORK_VARS}
    env["PAYPAL_ACCESS_TOKEN"] = token
    env["PAYPAL_ENVIRONMENT"] = "PRODUCTION" if os.getenv("PAYPAL_ENV") == "live" else "SANDBOX"
    return env

def start():
    """Start the background session. Returns at once and never raises."""
    global _thread, _loop
    try:
        if _thread is not None or not enabled(): return
        cmd, _ = _command()
        if not shutil.which(cmd):
            logging.info("PayPal MCP is off: %s isn't installed, so order lookups use the PayPal API directly", cmd)
            return
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(target=_run, args=(_loop,), name="paypal-mcp", daemon=True)
        _thread.start()
    except Exception:
        logging.exception("PayPal MCP could not start; using the PayPal API directly")

def stop():
    global _thread
    t, loop, ev = _thread, _loop, _stop_ev
    if t is None: return
    if loop is not None and ev is not None:
        try: loop.call_soon_threadsafe(ev.set)
        except RuntimeError: pass
    t.join(timeout=8)
    _thread = None

def _run(loop):
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_serve())
    except Exception:
        logging.exception("PayPal MCP stopped")
    finally:
        _ready.clear()

async def _wait_any(timeout, *events):
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks: t.cancel()

async def _serve():
    global _session, _stop_ev, _restart_ev
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        logging.info("PayPal MCP is off: run `pip install mcp` to turn it on")
        return
    _stop_ev, _restart_ev = asyncio.Event(), asyncio.Event()
    delay = 15
    while not _stop_ev.is_set():
        _restart_ev.clear()
        try:
            token = await asyncio.to_thread(paypal.fresh_token)
            cmd, args = _command()
            params = StdioServerParameters(command=cmd, args=args, env=_child_env(token))
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    await asyncio.wait_for(s.initialize(), 120)  # the first run may download the package
                    names = [t.name for t in (await s.list_tools()).tools]
                    if TOOL not in names: raise RuntimeError(f"the PayPal MCP server doesn't offer {TOOL} (it offers {names})")
                    _session = s; _ready.set(); delay = 15
                    logging.info("PayPal MCP server connected (read-only order lookups)")
                    # stay connected until asked to stop or reconnect, and reconnect with a new token before this one expires
                    await _wait_any(max(60.0, paypal.token_ttl() - 300), _stop_ev, _restart_ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("PayPal MCP session failed; using the PayPal API directly")
        finally:
            _ready.clear(); _session = None
        if _stop_ev.is_set(): break
        if _restart_ev.is_set(): continue  # a planned reconnect (new token): no waiting
        await _wait_any(delay, _stop_ev)
        delay = min(delay * 2, 300)

def _poke_restart():
    """Ask the session to reconnect with a fresh token (at most once a minute)."""
    global _last_poke
    if time.time() - _last_poke < 60 or _loop is None or _restart_ev is None: return
    _last_poke = time.time()
    try: _loop.call_soon_threadsafe(_restart_ev.set)
    except RuntimeError: pass

def _flag(res, *names):
    return any(bool(getattr(res, n, False)) for n in names)

def _text(res):
    return "".join(getattr(c, "text", "") or "" for c in (getattr(res, "content", None) or []))

def parse_order_json(text, order_id):
    """The server's text reply as PayPal's order dict; PayPalError if it isn't the order we asked for. Internal use only: it holds payer details."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i: raise paypal.PayPalError("MCP reply wasn't an order")
    try:
        d = json.loads(text[i:j + 1])
    except ValueError as e:
        raise paypal.PayPalError("MCP reply wasn't valid JSON") from e
    if not isinstance(d, dict) or not d.get("status"): raise paypal.PayPalError("MCP reply had no order status")
    if d.get("id") not in (None, order_id): raise paypal.PayPalError("MCP returned a different order")
    return d

def parse_order(text, order_id):
    """The same reply reduced to the safe summary the model may see."""
    s = paypal.order_summary(parse_order_json(text, order_id))
    if not s: raise paypal.PayPalError("MCP reply had no order status")
    return s

def get_order_json(order_id, timeout=15):
    """Look up one order through the MCP server and return PayPal's order dict (for the app's own checks). Raises paypal.PayPalError on any problem."""
    s, loop = _session, _loop
    if not _ready.is_set() or s is None or loop is None: raise paypal.PayPalError("PayPal MCP isn't connected")
    fut = asyncio.run_coroutine_threadsafe(s.call_tool(TOOL, {"id": order_id}), loop)
    try:
        res = fut.result(timeout)
    except Exception as e:  # a closed pipe, a timeout or a crashed server: reconnect, and let the caller use the API
        fut.cancel(); _poke_restart()
        raise paypal.PayPalError(f"PayPal MCP call failed ({type(e).__name__})") from e
    txt = _text(res)
    if _flag(res, "is_error", "isError"):
        if "401" in txt or "AUTH" in txt.upper(): _poke_restart()  # most likely an expired token
        raise paypal.PayPalError("PayPal MCP returned an error")
    return parse_order_json(txt, order_id)

def get_order(order_id, timeout=15):
    """Look up one order through the MCP server. Returns paypal.order_summary(); raises paypal.PayPalError on any problem."""
    s = paypal.order_summary(get_order_json(order_id, timeout))
    if not s: raise paypal.PayPalError("MCP reply had no order status")
    return s
