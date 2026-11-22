import json

import httpx
import pytest

from planning_aux import auth


SIGNIN = """<base href="/">
<div data-testid="local-signin-page" data-tenant-code="Ximi2/fasv"
data-redirect-url="authorize?request_uri=fresh&amp;sign-in-state=1"></div>"""
SIGNED_IN = '<form action="/Ximi2/fasv/Account/SignOut" method="post"></form>'


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("form_post", [False, True])
def test_authentication_keeps_cookies_and_follows_authorization(monkeypatch, form_post):
    monkeypatch.setattr(auth, "get_password", lambda: "test-password")
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == auth.ENTRY_URL:
            return httpx.Response(302, headers={
                "Location": auth.SSO + "/local-accounts/Ximi2%2Ffasv/SignIn",
                "Set-Cookie": "correlation=fresh; Path=/; Secure",
            })
        if request.url.path.endswith("/SignIn"):
            return httpx.Response(200, text=SIGNIN, headers={
                "Set-Cookie": "sso-context=fresh; Path=/; Secure",
            })
        if request.url.path == "/api/identity/sign-in":
            assert request.url.params["tenant-code"] == auth.TENANT
            assert request.headers["cookie"] == "sso-context=fresh"
            assert json.loads(request.content) == {"Login": "user", "Password": "test-password"}
            return httpx.Response(200, json={"succeeded": True}, headers={
                "Set-Cookie": "sso-session=ok; Path=/; Secure",
            })
        if request.url.path == "/authorize":
            assert request.url.params["request_uri"] == "fresh"
            assert "sso-session=ok" in request.headers["cookie"]
            if form_post:
                return httpx.Response(200, text=f"""
                <form method="post" action="{auth.EXTRANET}/signin-oidc">
                <input type="hidden" name="code" value="test-code">
                <input type="hidden" name="state" value="test-state">
                </form>""")
            return httpx.Response(302, headers={"Location": auth.EXTRANET + "/signin-oidc"})
        if request.url.path == "/signin-oidc":
            assert request.headers["cookie"] == "correlation=fresh"
            if form_post:
                assert request.method == "POST"
                assert request.content == b"code=test-code&state=test-state"
            return httpx.Response(302, headers={
                "Location": "/Ximi2/fasv",
                "Set-Cookie": "extranet-session=ok; Path=/; Secure",
            })
        assert request.url.path == "/Ximi2/fasv"
        assert "extranet-session=ok" in request.headers["cookie"]
        return httpx.Response(200, text=SIGNED_IN)

    with make_client(handler) as client:
        response = auth.authenticate(client, "user")
        assert response.url == auth.EXTRANET + "/Ximi2/fasv"
        assert client.cookies.get("extranet-session") == "ok"
    assert len([r for r in requests if r.method == "POST" and r.url.host == "sso.xelya.io"]) == 1


@pytest.mark.parametrize(("payload", "message"), [
    ({"succeeded": False}, "refusée"),
    ({"RequiresTwoFactor": True}, "double authentification"),
    ({"isLockedOut": True}, "verrouillé"),
    ({"IsNotAllowed": True}, "pas autorisée"),
    ({"succeeded": "true"}, "inattendue"),
    ([], "format inattendu"),
])
def test_rejected_login_stops_before_authorization(monkeypatch, payload, message):
    monkeypatch.setattr(auth, "get_password", lambda: "test-password")

    def handler(request):
        if str(request.url) == auth.ENTRY_URL:
            return httpx.Response(302, headers={"Location": auth.SSO + "/login"})
        if request.method == "GET":
            assert request.url.path == "/login"
            return httpx.Response(200, text=SIGNIN)
        return httpx.Response(200, json=payload)

    with make_client(handler) as client, pytest.raises(auth.AuthenticationError, match=message):
        auth.authenticate(client, "user")


@pytest.mark.parametrize("page", [
    "<html>Unexpected page</html>",
    SIGNIN.replace("Ximi2/fasv", "Other/tenant"),
    SIGNIN.replace("authorize?request_uri=fresh&amp;sign-in-state=1", "https://other.invalid/authorize"),
])
def test_invalid_signin_page_does_not_read_password(monkeypatch, page):
    def unexpected_password():
        pytest.fail("The password must not be read for an invalid page")

    monkeypatch.setattr(auth, "get_password", unexpected_password)

    def handler(request):
        assert request.method == "GET"
        if str(request.url) == auth.ENTRY_URL:
            return httpx.Response(302, headers={"Location": auth.SSO + "/login"})
        return httpx.Response(200, text=page)

    with make_client(handler) as client, pytest.raises(auth.AuthenticationError):
        auth.authenticate(client, "user")


def test_sso_success_without_authenticated_extranet_is_an_error(monkeypatch):
    monkeypatch.setattr(auth, "get_password", lambda: "test-password")

    def handler(request):
        if str(request.url) == auth.ENTRY_URL:
            return httpx.Response(302, headers={"Location": auth.SSO + "/login"})
        if request.url.path == "/login":
            return httpx.Response(200, text=SIGNIN)
        if request.method == "POST":
            return httpx.Response(200, json={"Succeeded": True})
        return httpx.Response(200, text="<html>Expired authorization</html>")

    with make_client(handler) as client, pytest.raises(auth.AuthenticationError, match="pas authentifié"):
        auth.authenticate(client, "user")


@pytest.mark.parametrize(("status", "body", "message"), [
    (429, "Too many requests", "HTTP 429"),
    (200, "<html>Service unavailable</html>", "JSON valide"),
])
def test_signin_protocol_errors_are_explicit(monkeypatch, status, body, message):
    monkeypatch.setattr(auth, "get_password", lambda: "test-password")

    def handler(request):
        if str(request.url) == auth.ENTRY_URL:
            return httpx.Response(302, headers={"Location": auth.SSO + "/login"})
        if request.method == "GET":
            assert request.url.path == "/login"
            return httpx.Response(200, text=SIGNIN)
        return httpx.Response(status, text=body)

    with make_client(handler) as client, pytest.raises(auth.AuthenticationError, match=message):
        auth.authenticate(client, "user")


def test_oidc_fields_are_not_posted_to_an_unknown_origin(monkeypatch):
    monkeypatch.setattr(auth, "get_password", lambda: "test-password")

    def handler(request):
        assert request.url.host in ("sso.xelya.io", "extranet.ximi.xelya.io")
        if str(request.url) == auth.ENTRY_URL:
            return httpx.Response(302, headers={"Location": auth.SSO + "/login"})
        if request.url.path == "/login":
            return httpx.Response(200, text=SIGNIN)
        if request.url.path == "/api/identity/sign-in":
            return httpx.Response(200, json={"Succeeded": True})
        assert request.method == "GET"
        return httpx.Response(200, text="""
        <form method="post" action="https://other.invalid/callback">
        <input type="hidden" name="code" value="test-code">
        <input type="hidden" name="state" value="test-state">
        </form>""")

    with make_client(handler) as client, pytest.raises(auth.AuthenticationError, match="inattendu"):
        auth.authenticate(client, "user")


@pytest.mark.parametrize("processing_fails", [False, True])
def test_session_logs_out_even_when_processing_fails(monkeypatch, processing_fails):
    signed_in = True
    logout_calls = []
    entry_calls = 0

    def handler(request):
        nonlocal signed_in, entry_calls
        if request.method == "POST":
            assert request.url == auth.EXTRANET + "/Ximi2/fasv/Account/SignOut"
            assert request.headers["cookie"] == "session=active"
            assert request.content == b"__RequestVerificationToken=fresh%2Btoken%26value"
            logout_calls.append(request)
            signed_in = False
            return httpx.Response(302, headers={"Location": auth.SSO + "/logout"})
        if request.url.path == "/Ximi2/fasv/Interventions/List":
            assert not signed_in
            return httpx.Response(302, headers={
                "Location": "/Ximi2/fasv/Account/SignIn?returnUrl=%2Ffasv",
            })
        if str(request.url) == auth.ENTRY_URL:
            entry_calls += 1
            if signed_in:
                token = "old-token" if entry_calls == 1 else "fresh+token&amp;value"
                return httpx.Response(200, text=SIGNED_IN.replace(
                    "</form>",
                    f'<input name="__RequestVerificationToken" type="hidden" value="{token}"></form>',
                ), headers={"Set-Cookie": "session=active; Path=/; Secure"})
            return httpx.Response(302, headers={"Location": auth.SSO + "/login"})
        assert request.url == auth.SSO + "/login"
        return httpx.Response(200, text=SIGNIN)

    client = make_client(handler)
    monkeypatch.setattr(auth.httpx, "Client", lambda **kwargs: client)

    def process():
        with auth.authenticated_session("user") as session:
            assert session is client
            assert signed_in
            if processing_fails:
                raise ValueError("Processing failed")

    if processing_fails:
        with pytest.raises(ValueError, match="Processing failed"):
            process()
    else:
        process()
    assert len(logout_calls) == 1
    assert not signed_in
    assert not client.cookies
    assert client.is_closed


def test_logout_does_not_post_without_csrf_token():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text=SIGNED_IN)

    with make_client(handler) as client, pytest.raises(auth.LogoutError, match="CSRF"):
        auth.logout(client)


@pytest.mark.parametrize("status", [403, 200])
def test_logout_failure_is_not_reported_as_success(status):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(status, text="Logout not performed")
        if request.url.path == "/Ximi2/fasv/Interventions/List":
            return httpx.Response(200, text=SIGNED_IN)
        return httpx.Response(200, text=SIGNED_IN.replace(
            "</form>",
            '<input name="__RequestVerificationToken" value="fresh-token"></form>',
        ))

    error = httpx.HTTPStatusError if status == 403 else auth.LogoutError
    with make_client(handler) as client, pytest.raises(error):
        auth.logout(client)


def test_session_closes_client_and_reports_logout_failure(monkeypatch):
    def handler(request):
        return httpx.Response(200, text=SIGNED_IN)

    client = make_client(handler)
    monkeypatch.setattr(auth.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(auth.LogoutError, match="CSRF"):
        with auth.authenticated_session("user"):
            pass
    assert client.is_closed
