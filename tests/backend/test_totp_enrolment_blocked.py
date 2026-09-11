"""2FA enrolment is refused until the login screen can prompt for a code.

The backend can generate a secret, confirm a code and verify one at login, but
LoginScreen.tsx only reports "not yet implemented" when a login answers
requires_totp — an account that turns TOTP on today loses browser access. The
enrolment endpoints therefore answer 501 and write nothing, while an account
that was enabled before this refusal keeps its second factor on API logins."""

import bcrypt
import pyotp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.database as db
from app.dependencies import get_current_user
from app.routers.auth import router as auth_router

EMAIL = "alice@example.com"
PASSWORD = "Correct-horse-9!"


# Mount only the router on a bare app so JWTAuthMiddleware is out of the path and
# the injected user exercises the route logic (same pattern as test_alerts_api).
@pytest.fixture
async def client(tmp_path, monkeypatch):
    await db.close_db()
    db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    try:
        await db.init_db()
        pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        assert await db.create_user(EMAIL, pw_hash, role="user", must_change_password=False)
        test_app = FastAPI()
        test_app.include_router(auth_router)
        test_app.dependency_overrides[get_current_user] = lambda: EMAIL
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://t") as c:
            yield c
    finally:
        await db.close_db()
        db._closed = False


async def test_setup_is_refused_and_writes_no_secret(client):
    r = await client.post("/api/auth/totp/setup")
    assert r.status_code == 501
    assert "login screen" in r.json()["detail"]
    user = await db.get_user(EMAIL)
    assert user["totp_secret"] is None
    assert user["totp_enabled"] is False


async def test_confirm_is_refused_even_with_a_planted_secret(client):
    # A secret written before the refusal, or by hand, must not be promotable.
    secret = pyotp.random_base32()
    await db.update_user(EMAIL, totp_secret=secret)
    r = await client.post("/api/auth/totp/confirm", json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 501
    assert (await db.get_user(EMAIL))["totp_enabled"] is False


async def test_login_still_requires_the_code_for_an_account_enabled_earlier(client):
    # Refusing new enrolments must not silently weaken accounts that already have it.
    secret = pyotp.random_base32()
    await db.update_user(EMAIL, totp_secret=secret, totp_enabled=1)
    r = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"requires_totp": True}
    r = await client.post(
        "/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD, "totp_code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200
    assert "access_token" in r.json()
