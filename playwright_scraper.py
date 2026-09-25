#!/usr/bin/env python3
"""
makemytrip-scraper — Playwright edition (primary engine)
========================================================

Scrapes makemytrip.com's hotel listings: one row per property, with the
price per night before and after taxes, the fees paid at the property, star
and guest ratings (and whose reviews they summarise), property type,
locality and coordinates.

    --mode hotels    (the only mode)  a city's hotel listing, for given
                                      dates and guests

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money. The whole fetch loop
lives in page_flow.py, once, and each engine supplies only how its driver
does each step.

What is different about MakeMyTrip
----------------------------------
* **Akamai refuses the ADDRESS, and it refuses it by hanging up.** Measured
  2026-09-24 from a datacentre VPS: curl gets Akamai's "Access Denied", and
  real Chromium, headless and headful, gets `net::ERR_HTTP2_PROTOCOL_ERROR`
  on every page. The Scraping Browser with an Indian exit (`country-in`)
  was served. So a run from a datacentre needs `--cdp-endpoint`, and the
  engine reports a dropped connection as a BLOCK (exit 3), not a timeout.
* **There is no captcha to solve.** None was met on any page, and Akamai's
  refusals carry no widget. Detection stays in place as insurance.
* **The data is the front end's own listing API**, called as a `fetch()`
  from a page on www.makemytrip.com with the headers the site's own front
  end sends. See product_parser's docstring.
* **The listing is paged by a cursor**, so pages are fetched one after
  another and --concurrency above 1 is refused.

Usage
-----
    python playwright_scraper.py --city Goa --checkin 2026-11-10 \\
        --checkout 2026-11-12 --pages 3 --cdp-endpoint "$MAKEMYTRIP_CDP_ENDPOINT"

    python playwright_scraper.py --city Dubai --sort price-asc --adults 1

    python playwright_scraper.py --url "https://www.makemytrip.com/hotels/hotel-listing/?checkin=11102026&checkout=11112026&city=CTGOI&locusId=CTGOI&locusType=city&country=IN&roomStayQualifier=2e0e"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import re
import sys
import time

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# Chromium's own names for a proxy that could not be used. Distinguished
# from a timeout because the two want opposite responses (CLAUDE.md §8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)

# The fetch() every page goes through. It is handed to `page.evaluate` as a
# FUNCTION, which Playwright sends through `Runtime.callFunctionOn` rather
# than evaluating a string, so it works under any Content-Security-Policy
# (§18). `credentials: "include"` sends the cookies Akamai's sensor set on
# the landing, as the site's own front end does. The AbortController is the
# timeout: a browser fetch() has none of its own, and §8 requires every
# remote call to be bounded.
FETCH_JS = """
async ([url, method, body, headers, timeoutMs]) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const init = {method, credentials: "include", signal: ctl.signal,
                  headers: Object.assign({"accept": "application/json"}, headers || {})};
    if (body !== null) {
      init.body = body;
    }
    const r = await fetch(url, init);
    return {status: r.status, text: await r.text(), waf: null};
  } catch (e) {
    return {status: 0, text: "", waf: null, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


def _chrome_ua(chromium_version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version (§8)."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    A proxy rotation tears the whole browser down and calls this again:
    cookies a bot manager issued against one exit, replayed from another,
    are a stronger signal than either address alone (§8).
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _Ops:
    """One browser + context + page, exposed as page_flow's named operations.

    page_flow owns the fetch loop for all three engines. This class answers
    only HOW Playwright does each step. `landed` records whether the page
    sits on a www.makemytrip.com document that fetch() can be issued from;
    the loop owns it, and a relaunch clears it.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None
        self.landed = False

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        self.landed = False
        return self

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except (PWTimeout, PWError) as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        if resp is None:
            return None, None
        return resp.status, None

    def document_text(self) -> str:
        try:
            return self.page.content()
        except PWError:
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self.page, self.args)

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.page.evaluate(
                FETCH_JS, [req.url, req.method, req.body_json, req.headers,
                           timeout_ms])
        except (PWError, PWTimeout) as e:
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit, or a fresh connection
        to the remote one. The second matters here: after a refused
        connection, the Scraping Browser served the reconnect from a
        different Indian exit (2026-09-24)."""
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        if self.remote:
            # The service releases a profile 1.6-1.9 s after a disconnect.
            time.sleep(page_flow.CDP_LOCKED_WAIT_S)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    browser, e = None, None
    for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            break
        except (PWError, PWTimeout) as err:
            e = err
            if attempt < page_flow.CDP_CONNECT_ATTEMPTS and page_flow.cdp_should_retry(str(err)):
                logger.warning("The Scraping Browser profile is still locked "
                               "(attempt %d/%d) — a previous run may be "
                               "releasing it; retrying in %.0fs.", attempt,
                               page_flow.CDP_CONNECT_ATTEMPTS,
                               page_flow.CDP_LOCKED_WAIT_S)
                time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                continue
            break
    if browser is None:
        # The endpoint carries a password, and Playwright repeats it five
        # times in its error text (§8). Rewritten with it masked, keeping
        # host and port, which are the useful half.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"{page_flow.cdp_connect_hint(str(e))}"
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # The Scraping Browser API's own CAPTCHA domain
    # (https://2captcha.com/scraper/browser-api/api). If a captcha ever
    # appears in front of the landing, the extension can clear it before the
    # local solver gets a turn.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY is the point: a Playwright connection error repeats the
# endpoint five times (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a reCAPTCHA on the current document. True if one was
    solved and the page reloaded.

    Only called for a landing page_flow classified as `challenge`: a captcha
    loader on a page the site did not otherwise serve. NOT OBSERVED on this
    site, so this path has never run against it; it is the family's broad
    detection kept as insurance. The static-HTML and live-page detectors are
    reconciled rather than short-circuited, because they can disagree about
    the variant and the parameters for one are rejected for the other (§8).

    This repo implements reCAPTCHA v2 and v3 here. Should the site ever
    render another kind, 2Captcha has task types for it and this client
    would need the matching one added: a TODO, not a limit (§19).
    """
    try:
        html = page.content()
    except PWError:
        return False
    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
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
    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading the landing.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    with sync_playwright() as pw:
        return page_flow.run_pages(
            lambda: _Ops(pw, args, pool, remote=bool(args.cdp_endpoint)).open(),
            lambda ops: ops.close(),
            args, pool, args.query, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="makemytrip.com hotel listing scraper (Playwright edition)")
    page_flow.add_arguments(p, engine="playwright")
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
                       "Scraping Browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
