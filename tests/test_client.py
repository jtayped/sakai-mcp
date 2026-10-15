import httpx
import pytest

from sakai_mcp.client import Sakai, SakaiError, parse_cookie_header


def test_parse_cookie_header():
    assert parse_cookie_header("JSESSIONID=abc.node1; _shib=x=y; ") == [("JSESSIONID", "abc.node1"),
                                                                        ("_shib", "x=y")]
    with pytest.raises(SakaiError):
        parse_cookie_header("abc.node1")


def test_cookie_session_is_used_and_not_renewed(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.headers.get("cookie"))
        return httpx.Response(200, json={"userId": None})

    s = Sakai("https://sakai.example.edu", None, None, tmp_path, cookie="JSESSIONID=abc")
    s.http._transport = httpx.MockTransport(handler)
    with pytest.raises(SakaiError, match="cookie has expired"):
        s.ensure()
    assert seen == ["JSESSIONID=abc"]
    assert Sakai("https://sakai.example.edu", None, None, tmp_path).http.cookies["JSESSIONID"] == "abc"


def test_redirect_off_host_is_refused_without_sending_the_cookie(tmp_path):
    seen = []

    def handler(req):
        seen.append((req.url.host, req.headers.get("cookie")))
        if req.url.path == "/direct/session/current.json":
            return httpx.Response(200, json={"userId": "u1"})
        if req.url.host == "sakai.example.edu":
            return httpx.Response(302, headers={"location": "https://tracker.example.com/x"})
        return httpx.Response(200, text="leaked")

    s = Sakai("https://sakai.example.edu", None, None, tmp_path, cookie="JSESSIONID=secret")
    s.http._transport = httpx.MockTransport(handler)
    with pytest.raises(SakaiError, match="refusing to follow a redirect"):
        s.request("GET", "/access/content/group/site/link.URL")
    assert all(host == "sakai.example.edu" for host, _ in seen)


def test_cookie_is_scoped_to_the_sakai_host(tmp_path):
    s = Sakai("https://sakai.example.edu", None, None, tmp_path, cookie="JSESSIONID=secret")
    assert [c.domain for c in s.http.cookies.jar] == ["sakai.example.edu"]
    assert (tmp_path / "cookies-sakai.example.edu.json").stat().st_mode & 0o777 == 0o600


def test_forbidden_with_a_live_session_does_not_log_in_again(tmp_path):
    def handler(req):
        if req.url.path == "/direct/session/current.json":
            return httpx.Response(200, json={"userId": "u1"})
        return httpx.Response(403, text="no permission")

    s = Sakai("https://sakai.example.edu", "user", "pw", tmp_path)
    s.http._transport = httpx.MockTransport(handler)
    s.login = lambda: pytest.fail("logged in again on a permission error")
    with pytest.raises(SakaiError, match="HTTP 403"):
        s.request("GET", "/direct/site/x/memberships.json")


def test_html_error_body_is_reduced_to_text(tmp_path):
    page = "<!doctype html><html><head><style>h1 {color: red}</style></head><body><h1>HTTP Status 404</h1></body></html>"

    def handler(req):
        if req.url.path == "/direct/session/current.json":
            return httpx.Response(200, json={"userId": "u1"})
        return httpx.Response(404, text=page)

    s = Sakai("https://sakai.example.edu", "user", "pw", tmp_path)
    s.http._transport = httpx.MockTransport(handler)
    with pytest.raises(SakaiError) as e:
        s.request("GET", "/direct/assignment/item/x.json")
    assert str(e.value) == "GET /direct/assignment/item/x.json: HTTP 404: HTTP Status 404"
