#!/usr/bin/env python3
"""
makemytrip-scraper — Selenium edition (secondary engine)
========================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The fetch loop that decides all three lives in page_flow.py
and is shared, so this file is browser plumbing and nothing else.

    --mode hotels    (the only mode)  a city's hotel listing

Three limits of this engine, stated here rather than left to be discovered.
None is a bug in this code and none can be fixed from here, and on THIS
site the first one decides everything:

  * **Selenium cannot use an authenticated remote CDP endpoint.**
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password, so a credentialled --cdp-endpoint (the Scraping
    Browser API) is refused with exit 2 rather than connected to and
    silently failing. And the Scraping Browser is the one path measured to
    get past Akamai from a datacentre (2026-09-24). So from a datacentre this
    engine is refused on every run, correctly reported as exit 3; it can
    only work from an address Akamai serves, such as a home connection in
    India, which has not been measured.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=`
    accepts no credentials. They are stripped and a warning says so.
  * **Selenium reports no HTTP status, and does not raise on a network
    error.** Chrome renders its own error page instead, carrying the site's
    hostname as its title (§18). `goto` therefore reads the document for
    Chrome's error names and raises them, so a connection Akamai dropped is
    reported as the refusal it is (exit 3), exactly as in the twins.

Usage
-----
    python selenium_scraper.py --city Goa --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import re
import sys
import time
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ProxyError,
                        split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

PAGE_LOAD_TIMEOUT = 60
# Longer than the fetch() timeout, so the page's own AbortController reports
# a slow request (as a transport error the loop retries) before Selenium's
# script timeout cuts the call off with an exception.
SCRIPT_TIMEOUT = page_flow.FETCH_TIMEOUT_MS // 1000 + 15

# Chromium's own names for "the proxy is the problem, not the site" (§8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES", "ERR_SOCKS_CONNECTION_FAILED",
)

# Selenium's dialect of the fetch() in playwright_scraper.FETCH_JS: a
# function BODY run by execute_async_script, with the arguments in
# `arguments` and the result handed to the callback Selenium appends last.
# Same request, same headers, same return shape, same AbortController
# timeout (§8).
FETCH_JS = """
var done = arguments[arguments.length - 1];
var url = arguments[0], method = arguments[1], body = arguments[2];
var headers = Object.assign({"accept": "application/json"}, arguments[3] || {});
var ctl = new AbortController();
var timer = setTimeout(function () { ctl.abort(); }, arguments[4]);
var init = {method: method, credentials: "include", signal: ctl.signal,
            headers: headers};
if (body !== null) {
  init.body = body;
}
fetch(url, init).then(function (r) {
  return r.text().then(function (t) {
    clearTimeout(timer);
    done({status: r.status, text: t, waf: null});
  });
}).catch(function (e) {
  clearTimeout(timer);
  done({status: 0, text: "", waf: null, error: String(e)});
});
"""


# Every `scheme://user:pass@` in a string, however many times it occurs (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the installed Chrome's own version (§8)."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _proxy_failure(text) -> str:
    text = str(text)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""



def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Ops:
    """One Chrome driver, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser (§8).
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None
        self.landed = False

    def open(self):
        self.landed = False
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own (§8).
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"--lang={self.args.locale}")
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only. They have "
                    "been stripped, so the exit will most likely refuse the "
                    "requests. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)
        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs
        # the run (§8).
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install."""
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": playwright_init_script(fp)})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            self.driver.get(url)
        except WebDriverException as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        # No status and no headers from a Selenium navigation, and no
        # exception for a network error either: Chrome shows its own error
        # page. Its error NAME is in that page, so surface it as the twins'
        # drivers would have raised it.
        failure = _chrome_error(self.driver)
        if failure:
            raise page_flow.TransportError("net::%s at %s" % (failure, url))
        return None, None

    def document_text(self) -> str:
        try:
            return self.driver.page_source or ""
        except WebDriverException:
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self, self.args)

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.driver.execute_async_script(
                FETCH_JS, req.url, req.method, req.body_json, req.headers,
                timeout_ms)
        except WebDriverException as e:
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() leaves the driver process
                # running, which a per-page rotation would leak once a page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


# Chromium's network-error page states the failure in its own page data as
# `"errorCode":"ERR_HTTP2_PROTOCOL_ERROR"`. The page also carries other ERR_
# names in its templates (ERR_INTERNET_DISCONNECTED, on the one captured
# 2026-09-24), so the first ERR_ string on it is NOT the error.
_CHROME_ERROR_RE = re.compile(r'"errorCode"\s*:\s*"(ERR_[A-Z0-9_]{4,60})"')


def _chrome_error(driver) -> str:
    """The Chromium error name on the current document, or "".

    Only read when the document IS Chrome's error page, and that is decided
    by the page's OWN `location.href`: Selenium's `current_url` reports the
    address that was requested (measured: www.makemytrip.com/hotels/) while
    the page itself sits at chrome-error://chromewebdata/. A real page could
    mention an ERR_ string in its scripts, so nothing else is trusted."""
    try:
        href = driver.execute_script("return location.href") or ""
        if not href.startswith("chrome-error://"):
            return ""
        m = _CHROME_ERROR_RE.search(driver.page_source or "")
    except WebDriverException:
        return ""
    return m.group(1) if m else "ERR_FAILED"


def handle_captcha_if_present(ops, args) -> bool:
    """Detect and solve a reCAPTCHA on the current document. Mirrors
    playwright_scraper.handle_captcha_if_present: called only for a landing
    classified as `challenge`, which has not been observed on this site."""
    try:
        html = ops.driver.page_source
    except WebDriverException:
        return False
    url = ops.driver.current_url
    html_challenge = detect_recaptcha_v3(html, url)
    # execute_script takes a function BODY with an explicit return, so the
    # discovery arrow function is wrapped and invoked (§1).
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: ops.driver.execute_script("return (%s)();" % js), page_url=url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        logger.warning("The landing carries a captcha loader, but no widget "
                       "this repo implements was found on it — not sending "
                       "anything to the solver.")
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key — cannot solve it. Set "
                       "TWOCAPTCHA_KEY in .env.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver error is a warning (§8)
        logger.error("Solving the captcha failed (%s) — continuing.",
                     _mask_credentials(str(e)))
        return False
    ops.driver.execute_script("return (%s)(arguments[0]);" % INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading the landing.")
    time.sleep(1.5)
    ops.driver.refresh()
    return True


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    return page_flow.run_pages(
        lambda: _Ops(args, pool).open(),
        lambda ops: ops.close(),
        args, pool, args.query, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="makemytrip.com hotel listing scraper (Selenium edition)")
    page_flow.add_arguments(p, engine="selenium")
    args = p.parse_args(argv)
    env_config.apply(args)
    if args.category is not None:
        p.error("--category: this site has no category to pick; pass --city.")
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except WebDriverException as e:
        # A remote browser that will not accept the attachment is a REMOTE
        # failure (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if args.cdp_endpoint and ("cannot connect" in text.lower()
                                  or "debugger" in text.lower()):
            logger.error("Could not attach to --cdp-endpoint: %s", text)
            sys.exit(EXIT_API_ERROR)
        raise
