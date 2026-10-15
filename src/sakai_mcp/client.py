"""HTTP session for one Sakai user: password login, cookie reuse across restarts, and
re-login when the session expires. On SSO-only servers there is no password to log in
with, so the session cookie is copied from a browser instead and can't be renewed here.

Sakai answers anonymous /direct requests with 200 and empty collections instead of an
auth error, so an expired session can't be detected from the response. `ensure()`
re-checks /direct/session/current.json at most every CHECK_EVERY seconds instead.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

UA = "sakai-mcp (+https://github.com/jtayped/sakai-mcp)"
CHECK_EVERY = 300


# A ToolError keeps its message on the way to the client; any other exception is reported as a crash.
class SakaiError(ToolError):
    pass


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    """Split a browser Cookie header ("JSESSIONID=abc; other=x") into name/value pairs."""
    pairs = []
    for part in filter(None, (p.strip() for p in header.split(";"))):
        name, eq, value = part.partition("=")
        if not eq or not name.strip():
            raise SakaiError("SAKAI_COOKIE must look like a Cookie header: NAME=value; NAME2=value2")
        pairs.append((name.strip(), value.strip()))
    return pairs


class Sakai:
    def __init__(self, base_url: str, username: str | None, password: str | None, state_dir: Path,
                 cookie: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.host = urlsplit(self.base_url).hostname
        self.username, self.password = username, password
        self.cookie_file = state_dir / f"cookies-{self.host}.json"
        # MCP runs tool calls in parallel threads; a re-login mid-submission would drop the tool's form state.
        self.lock = threading.RLock()
        self.http = httpx.Client(base_url=self.base_url, follow_redirects=True, timeout=60,
                                 headers={"User-Agent": UA, "Accept": "application/json, */*"},
                                 event_hooks={"request": [self._same_host_only]})
        if self.cookie_file.exists():
            for name, value in json.loads(self.cookie_file.read_text()).items():
                self.http.cookies.set(name, value, domain=self.host)
        if cookie:
            for name, value in parse_cookie_header(cookie):
                self.http.cookies.set(name, value, domain=self.host)
            self._save_cookies()
        self.user_id: str | None = None
        self.checked_at = 0.0

    @classmethod
    def from_env(cls) -> "Sakai":
        env = os.environ.get
        if not env("SAKAI_URL"):
            raise SakaiError("missing environment variable SAKAI_URL")
        if not env("SAKAI_COOKIE") and not (env("SAKAI_USERNAME") and env("SAKAI_PASSWORD")):
            raise SakaiError("set SAKAI_USERNAME and SAKAI_PASSWORD, or SAKAI_COOKIE on SSO-only servers")
        state = Path(env("SAKAI_STATE_DIR") or Path.home() / ".local/state/sakai-mcp")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cls(env("SAKAI_URL"), env("SAKAI_USERNAME"), env("SAKAI_PASSWORD"), state, env("SAKAI_COOKIE"))

    def _same_host_only(self, req: httpx.Request):
        # Redirects (e.g. Sakai web-link resources) must not carry the session to, or fetch from, other hosts.
        if req.url.host != self.host:
            raise SakaiError(f"refusing to follow a redirect off {self.host} to {req.url}")

    def _save_cookies(self):
        fd = os.open(self.cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({c.name: c.value for c in self.http.cookies.jar}, f)

    def _current_user(self) -> str | None:
        r = self.http.get("/direct/session/current.json")
        return r.json().get("userId") if r.is_success else None

    def login(self):
        if not self.password:
            raise SakaiError("the Sakai session cookie has expired or is invalid; log in to Sakai in a browser, "
                             "copy its cookies into SAKAI_COOKIE and restart the server")
        self.http.cookies.clear()
        # /direct/session exists on every Sakai with EntityBroker; /api/login only on newer releases.
        attempts = [("/direct/session", {"_username": self.username, "_password": self.password}),
                    ("/api/login", {"username": self.username, "password": self.password})]
        for path, form in attempts:
            r = self.http.post(path, data=form)
            if r.is_success and (uid := self._current_user()):
                self.user_id, self.checked_at = uid, time.monotonic()
                self._save_cookies()
                return
        raise SakaiError(f"login failed for {self.username} (last answer HTTP {r.status_code} from {path}); "
                         "the server may only accept SSO logins")

    def ensure(self):
        with self.lock:
            if self.user_id and time.monotonic() - self.checked_at < CHECK_EVERY:
                return
            if uid := self._current_user():
                self.user_id, self.checked_at = uid, time.monotonic()
            else:
                self.login()

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        with self.lock:
            return self._request(method, path, **kw)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        self.ensure()
        r = self.http.request(method, path, **kw)
        # /direct also answers 403 for a valid session without permission, so only re-login if it's gone.
        expired = (r.status_code == 401 or "/sakai-login-tool/" in str(r.url)
                   or (r.status_code == 403 and not self._current_user()))
        if expired:
            self.login()
            r = self.http.request(method, path, **kw)
        if r.status_code == 404 and path.startswith("/api/"):
            raise SakaiError(f"{method} {path}: 404, this Sakai version probably has no such /api route")
        if not r.is_success:
            raise SakaiError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r

    def get_json(self, path: str, **params):
        r = self.request("GET", path, params=params or None)
        try:
            return r.json()
        except ValueError:
            raise SakaiError(f"GET {path} did not return JSON: {r.text[:200]}") from None

    def version(self) -> str | None:
        """version.sakai from server-config, which some installs hide from non-admins."""
        try:
            return self.get_json("/direct/server-config/version.sakai.json").get("value")
        except SakaiError:
            return None
