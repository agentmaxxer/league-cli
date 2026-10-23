"""HTTP client for League of Comic Geeks using Playwright with real Chrome."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from playwright.sync_api import APIResponse, sync_playwright

from locg.config import cookie_path, playwright_profile_dir

BASE_URL = "https://leagueofcomicgeeks.com"

logger = logging.getLogger("locg")


class AuthRequired(Exception):
    """Raised when a command requires authentication."""
    pass


class CloudflareChallenge(Exception):
    """Raised when Cloudflare still serves a challenge after one re-warm."""
    pass


# Default user-agent strings. Cloudflare refuses the "HeadlessChrome" UA that
# headless Chrome advertises, so headless runs pretend to be a normal Chrome.
# Override with LOCG_USER_AGENT when your installed Chrome is much newer.
_DEFAULT_UA = {
    "darwin": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "win32": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    "linux": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
}

# Markers of a Cloudflare interstitial / Turnstile page.
# (Normal League pages also load /cdn-cgi/challenge-platform scripts, so that
# path alone is not a marker.)
_CF_MARKERS = (
    "<title>just a moment",
    "<title>attention required",
    "cf_chl_opt",
    "cf-chl-",
)

# Seconds to wait for the Turnstile challenge on the homepage to clear.
_TURNSTILE_WAIT_SECONDS = 50


def _browser_mode() -> tuple[bool, list[str]]:
    """Return ``(headless, extra_args)`` for this platform.

    Windows: Cloudflare refuses hidden Chrome there, so run a real window
    parked off-screen. Elsewhere: headless. ``LOCG_HEADLESS=0|1`` overrides.
    """
    env = os.environ.get("LOCG_HEADLESS")
    if env is not None:
        headless = env.strip().lower() not in ("0", "false", "no")
    else:
        headless = not sys.platform.startswith("win")
    extra: list[str] = []
    if not headless:
        extra = ["--window-position=-32000,-32000", "--window-size=1200,800"]
    return headless, extra


def _user_agent() -> str:
    env = os.environ.get("LOCG_USER_AGENT")
    if env:
        return env
    if sys.platform.startswith("win"):
        return _DEFAULT_UA["win32"]
    if sys.platform == "darwin":
        return _DEFAULT_UA["darwin"]
    return _DEFAULT_UA["linux"]


def looks_like_challenge(status: int, text: str) -> bool:
    """True when a response is a Cloudflare challenge rather than real data.

    League's API answers JSON (``{...}``) or HTML pages; a 403/503 or an HTML
    body carrying Cloudflare's challenge markers is a challenge, not a miss.
    """
    if status in (403, 503):
        return True
    head = (text or "")[:20000].lower()
    if head.lstrip().startswith("{") or head.lstrip().startswith("["):
        return False
    return any(m in head for m in _CF_MARKERS) and "<html" in head


@dataclass
class _PlaywrightResponse:
    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.text)


def _wrap_response(api_response: APIResponse) -> _PlaywrightResponse:
    try:
        raw = api_response.body()
        return _PlaywrightResponse(
            status_code=api_response.status,
            text=raw.decode("utf-8", errors="replace"),
            content=raw,
            headers=dict(api_response.headers),
        )
    finally:
        api_response.dispose()


class LOCGClient:
    """HTTP client that uses real Chrome via Playwright to bypass Cloudflare."""

    def __init__(self) -> None:
        self._playwright_instance = sync_playwright().start()
        try:
            profile_dir = str(playwright_profile_dir())
            headless, extra_args = _browser_mode()
            self._context = self._playwright_instance.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=headless,
                user_agent=_user_agent(),
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    *extra_args,
                ],
            )
            self._page = self._context.new_page()
        except Exception:
            self._playwright_instance.stop()
            raise
        self._server_auth_verified: Optional[bool] = None
        self._cf_warmed_up = False
        # Remove stale cookies.json left by the previous curl_cffi implementation.
        p = cookie_path()
        if p.exists():
            try:
                p.unlink()
                logger.debug("Removed legacy cookies.json (cookies now in Playwright profile)")
            except OSError:
                pass

    def _warm_up_cloudflare(self) -> None:
        """Navigate to homepage so Cloudflare's Turnstile challenge can run and set cf_clearance."""
        if self._cf_warmed_up:
            return
        logger.debug("Warming up Cloudflare clearance via homepage navigation")
        # networkidle never settles on League (ads); wait for the DOM and then
        # poll the title until the Turnstile interstitial is gone.
        self._page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(_TURNSTILE_WAIT_SECONDS):
            title = (self._page.title() or "").lower()
            if title and "moment" not in title and "restricted" not in title and "attention" not in title:
                break
            time.sleep(1)
        else:
            logger.warning("Cloudflare challenge did not clear within %ss", _TURNSTILE_WAIT_SECONDS)
        self._cf_warmed_up = True
        logger.debug("Cloudflare warm-up complete")

    @property
    def is_authenticated(self) -> bool:
        cookies = self._context.cookies([BASE_URL])
        return any(c["name"] == "ci_session" for c in cookies)

    def require_auth(self) -> None:
        if not self.is_authenticated:
            if self._try_env_login():
                return
            raise AuthRequired("Not logged in. Run: locg login")
        if self._server_auth_verified is None:
            # verify_session may raise (429, network, malformed response).
            # Do NOT cache the result on failure — let the exception
            # propagate so the next invocation retries.
            self._server_auth_verified = self.verify_session()
        if self._server_auth_verified is False:
            if self._try_env_login():
                return
            raise AuthRequired("Session expired. Run: locg login")

    def _try_env_login(self) -> bool:
        """Attempt auto-login using LOCG_USERNAME/LOCG_PASSWORD from the environment.

        Returns True if login succeeded, False if credentials are missing or
        the login failed. Callers treat False as "fall back to raising AuthRequired".

        Exceptions from login() (rate limits, network errors) are caught and
        logged so callers see a clean AuthRequired rather than a raw traceback
        propagating to exit code 4.
        """
        username = os.environ.get("LOCG_USERNAME")
        password = os.environ.get("LOCG_PASSWORD")
        if not username or not password:
            return False
        logger.debug("Attempting auto-login from LOCG_USERNAME/LOCG_PASSWORD")
        try:
            ok = self.login(username, password)
        except Exception as e:
            logger.warning("Auto-login failed: %s", e)
            return False
        if ok:
            # login() sets _server_auth_verified internally, but make the
            # post-condition explicit so require_auth's early-return paths
            # don't depend on that side effect.
            self._server_auth_verified = True
        else:
            logger.warning(
                "Auto-login rejected by server. "
                "Check LOCG_USERNAME / LOCG_PASSWORD."
            )
        return ok

    def _page_fetch(
        self, url: str, method: str = "GET", form: Optional[dict[str, Any]] = None,
    ) -> _PlaywrightResponse:
        """Run ``fetch()`` inside the cleared page.

        Cloudflare challenges Playwright's ``page.request`` channel even with a
        valid clearance cookie, while a fetch from the page itself passes.
        """
        if not self._cf_warmed_up:
            self._warm_up_cloudflare()
        js = """async ([url, method, form]) => {
            const opts = {method, credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}};
            if (form && method !== "GET") {
                const b = new URLSearchParams();
                for (const [k, v] of Object.entries(form)) b.append(k, v);
                opts.body = b;
            }
            const r = await fetch(url, opts);
            const h = {}; r.headers.forEach((v, k) => { h[k] = v; });
            return [r.status, await r.text(), h];
        }"""
        payload = {k: str(v) for k, v in (form or {}).items()}
        status, text, headers = self._page.evaluate(js, [url, method, payload])
        return _PlaywrightResponse(
            status_code=status, text=text, content=text.encode("utf-8"), headers=headers,
        )

    def _request(
        self, method: str, url: str, form: Optional[dict[str, Any]] = None,
    ) -> _PlaywrightResponse:
        """Fetch with Cloudflare detection: re-warm and retry once, then fail clearly."""
        logger.debug(f"{method} {url}")
        for attempt in (1, 2):
            start = time.monotonic()
            resp = self._page_fetch(url, method, form)
            elapsed = time.monotonic() - start
            logger.debug(f"  -> {resp.status_code} ({elapsed:.2f}s, {len(resp.content)} bytes)")
            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after", "60")
                logger.warning(f"Rate limited on {method} {url}, retry after {retry_after}s")
                raise Exception(f"Rate limited. Retry after {retry_after}s")
            if not looks_like_challenge(resp.status_code, resp.text):
                return resp
            if attempt == 1:
                # Clearance missing or expired mid-session: re-warm, retry once.
                logger.info("Cloudflare challenge on %s %s; re-warming and retrying", method, url)
                self._cf_warmed_up = False
                self._warm_up_cloudflare()
        raise CloudflareChallenge(
            "Cloudflare challenge persisted after a re-warm (HTTP "
            f"{resp.status_code}). This is not a miss: the clearance is dead. "
            "Wait a minute and retry; if it keeps happening, open the site once "
            "in a normal browser window with LOCG_HEADLESS=0."
        )

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> _PlaywrightResponse:
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        return self._request("GET", url)

    def post(self, path: str, data: Optional[dict[str, Any]] = None) -> _PlaywrightResponse:
        return self._request("POST", f"{BASE_URL}{path}", data or {})

    def verify_session(self) -> bool:
        """Confirm session validity from local cookie state. No HTTP call.

        The previous implementation fetched the entire user collection
        (~545 KB) on every authenticated command just to read ``data-user``.
        A second attempt used a small probe URL, but Cloudflare flags
        unusual probe titles, making any explicit GET unreliable.

        Server-side staleness (cookie present but invalidated) surfaces
        downstream: list commands trip ``_check_session_valid``, and any
        command's response that contains ``data-user="0"`` raises
        AuthRequired with the standard "Session expired" message.
        """
        return self.is_authenticated

    def login(self, username: str, password: str) -> bool:
        """Log in and persist the session cookie. Returns True on success."""
        resp = self.post("/login", data={
            "username": username,
            "password": password,
        })
        if not self.is_authenticated:
            logger.debug(f"Login failed: no ci_session cookie (status {resp.status_code})")
            return False

        # Verify the session is actually valid server-side
        if not self.verify_session():
            logger.debug("Login appeared to succeed but session is not valid server-side")
            return False

        self._server_auth_verified = True
        logger.debug("Login successful (verified)")
        return True

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright_instance.stop()
