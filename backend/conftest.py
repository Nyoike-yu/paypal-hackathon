"""One Postgres for every test file, set up before `main` is first imported.

By default it is an embedded one (pgserver, from requirements-dev.txt). If pgserver won't install on your machine, set
TEST_DATABASE_URL to an EMPTY, throwaway Postgres database instead (local Postgres, Docker, or a spare Supabase project).
The app's tables in that database are dropped and recreated on every run, so never point it at your real data.
"""
import os, tempfile
import psycopg
from dotenv import dotenv_values

HERE = os.path.dirname(__file__)
_test_url = os.getenv("TEST_DATABASE_URL")
if _test_url:
    _real = os.getenv("DATABASE_URL") or dotenv_values(os.path.join(HERE, ".env")).get("DATABASE_URL")
    if _real and _test_url.strip() == _real.strip():
        raise RuntimeError("TEST_DATABASE_URL is the same as DATABASE_URL. The tests wipe their database, so use a separate, empty one.")
    _uri = _test_url
    with psycopg.connect(_uri, autocommit=True) as _c:
        _c.execute("drop table if exists refunds, push_subs, missions, proposals, pay_orders, alerts, audit, purchases, goals, rules, products, profiles cascade")
else:
    import pgserver
    _srv = pgserver.get_server(tempfile.mkdtemp())
    _uri = _srv.get_uri()
with psycopg.connect(_uri, autocommit=True) as _c:
    _c.execute(open(os.path.join(HERE, "schema.sql")).read())
os.environ.update(DATABASE_URL=_uri, SECRET_KEY="k" * 40, PENNY_AI="off")
for _k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "PAYPAL_CLIENT_ID", "PAYPAL_SECRET", "PAYPAL_MCP"):
    os.environ.pop(_k, None)

import pytest

@pytest.fixture(scope="session")
def client():
    """One running app for the whole test session (its connection pool can't be reopened once closed)."""
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c
