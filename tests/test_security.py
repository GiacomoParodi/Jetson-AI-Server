from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import login_limiter


def test_security_headers_on_pages_and_api(client):
    for url in ("/", "/api/health", "/static/app.js"):
        r = client.get(url)
        assert r.headers["x-content-type-options"] == "nosniff", url
        assert r.headers["x-frame-options"] == "DENY", url
        assert r.headers["referrer-policy"] == "no-referrer", url
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self'" in csp, url
        assert "strict-transport-security" not in r.headers  # solo su HTTPS
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_hsts_only_over_https(client):
    secure = TestClient(app, base_url="https://testserver")
    assert "max-age" in secure.get("/api/health").headers["strict-transport-security"]


def test_streaming_and_images_keep_their_own_cache_rules(client, login):
    h = login("mario")
    # Le intestazioni di sicurezza non sovrascrivono quelle già impostate dalle singole risposte.
    r = client.get("/api/me", headers=h)
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"


def test_api_docs_are_off_by_default(client):
    assert settings.api_docs is False
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(url).status_code == 404, url


def test_default_bind_is_local_only():
    assert settings.host == "127.0.0.1"


def test_failed_logins_do_not_lock_other_users(client, users):
    login_limiter.clear()
    for _ in range(5):
        assert client.post("/api/login", json={"username": "mario", "password": "sbagliata"}).status_code == 401
    assert client.post("/api/login", json={"username": "mario", "password": users["mario"]}).status_code == 429
    # Un altro utente, dallo stesso indirizzo, entra senza problemi.
    assert client.post("/api/login", json={"username": "luigi", "password": users["luigi"]}).status_code == 200
    # Maiuscole e spazi non permettono di aggirare il blocco.
    assert client.post("/api/login", json={"username": " MARIO ", "password": "x"}).status_code == 429
    login_limiter.clear()
    client.cookies.clear()


def test_sessions_are_stored_hashed_and_cookie_flags(client, users):
    from app import db

    login_limiter.clear()
    secure = TestClient(app, base_url="https://testserver")
    r = secure.post("/api/login", json={"username": "mario", "password": users["mario"]})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
    token = r.json()["token"]
    assert db.get_session_user(token) is None  # nel database c'è solo l'hash, non il token
    assert db.get_session_user(__import__("app.security", fromlist=["hash_token"]).hash_token(token)) is not None
    login_limiter.clear()
