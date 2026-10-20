import time

import pytest

from app import auth, database
from conftest import register


def test_case_variants_cannot_alias_another_account(client):
    register(client, "alice")
    for name in ("ALICE", "Alice", "KEXIKY", "Kexiky"):
        r = client.post("/api/auth/register", json={"username": name, "password": "secret123"})
        assert r.status_code == 400, name


def test_token_resolves_only_to_its_own_account(client):
    rec = database.register_user("bob", "secret123")
    good = auth.create_user_token(rec["id"], "bob", "user")
    assert auth.verify_user_token(good)["username"] == "bob"
    # same id, different spelling of the name -> rejected
    assert auth.verify_user_token(auth.create_user_token(rec["id"], "BOB", "user")) is None
    # name of the creator with someone else's id -> rejected
    assert auth.verify_user_token(auth.create_user_token(rec["id"], "kexiky", "admin")) is None


def test_legacy_case_collision_poc_no_longer_escalates(client):
    # The original attack: register a case variant of the creator and use the issued token.
    r = client.post("/api/auth/register", json={"username": "KEXIKY", "password": "secret123"})
    assert r.status_code == 400
    me = client.get("/api/auth/me").json()
    assert me["role"] == "guest" and not me["is_admin"]


def test_password_change_revokes_old_tokens(client):
    rec = database.register_user("carol", "secret123")
    old = auth._sign_payload(f"mp:{rec['id']}:carol:user:{int(time.time()) - 30}")
    assert auth.verify_user_token(old) is not None
    database.update_user_password("carol", "secret123", "another-secret")
    assert auth.verify_user_token(old) is None
    fresh = auth.create_user_token(rec["id"], "carol", "user")
    assert auth.verify_user_token(fresh) is not None


def test_forged_guest_cookie_is_ignored(client):
    client.cookies.set("mediapull_session", "guest_aaaaaaaaaa")
    me = client.get("/api/auth/me").json()
    assert me["user_id"] != "guest_aaaaaaaaaa"
    assert me["is_new_session"] is True


def test_guest_session_cookie_is_signed_and_reused(client):
    first = client.get("/api/auth/me").json()
    assert first["is_new_session"] and "session_token" not in first
    cookie = client.cookies.get("mediapull_session")
    assert cookie and "." in cookie
    second = client.get("/api/auth/me").json()
    assert second["user_id"] == first["user_id"] and not second["is_new_session"]


@pytest.mark.parametrize("name", ["Алексей", "ab", "guest_abc123", "tg_user", "a b c", "x" * 40, "ev!l"])
def test_invalid_usernames_rejected(client, name):
    r = client.post("/api/auth/register", json={"username": name, "password": "secret123"})
    assert r.status_code == 400


def test_short_password_rejected(client):
    r = client.post("/api/auth/register", json={"username": "dave", "password": "12345"})
    assert r.status_code == 400


def test_login_is_rate_limited(client):
    register(client, "erin")
    codes = [client.post("/api/auth/login", json={"username": "erin", "password": "wrong-pass"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert 429 in codes[10:]


def test_login_success_and_cookie_flags(client):
    register(client, "frank")
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": "FRANK", "password": "secret123"})
    assert r.status_code == 200 and r.json()["username"] == "frank"
    assert "httponly" in r.headers["set-cookie"].lower()


def test_new_guest_sessions_are_capped_per_ip(make_client, monkeypatch):
    monkeypatch.setattr(auth, "_new_guest_limiter", auth.SlidingWindowLimiter(3, 3600))
    codes = [make_client().get("/api/auth/me").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_health_does_not_create_sessions(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert "mediapull_session" not in client.cookies
