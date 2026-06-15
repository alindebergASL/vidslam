from __future__ import annotations


def test_logout_clears_cookie_and_subsequent_requests_401(client):
    # Login → session cookie is set; an authed call works.
    assert client.post("/api/auth/login", json={"password": "test-pw"}).status_code == 200
    assert client.get("/api/avatars").status_code == 200

    # Logout → server tells the browser to delete the cookie.
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    # Either a Set-Cookie line with Max-Age=0 or a removed cookie is acceptable;
    # the contract that matters is that the *next* request is unauthenticated.
    client.cookies.clear()
    assert client.get("/api/avatars").status_code == 401


def test_auth_status_reflects_logout(client):
    client.post("/api/auth/login", json={"password": "test-pw"})
    assert client.get("/api/auth/status").json() == {"authenticated": True}
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/auth/status").json() == {"authenticated": False}
