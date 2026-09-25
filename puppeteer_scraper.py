#!/usr/bin/env python3
"""
makemytrip-scraper — pyppeteer edition (secondary engine)
=========================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The fetch loop that decides all three lives in page_flow.py
and is shared, so this file is browser plumbing and nothing else: how
pyppeteer navigates, reads a document and issues a fetch().

    --mode hotels    (the only mode)  a city's hotel listing

See playwright_scraper.py's header for why every request is a fetch() from
a landed page rather than a rendered listing, and why a datacentre run needs
--cdp-endpoint.

Two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
  * Unlike the Selenium engine, it CAN authenticate a remote CDP endpoint
    (`browserWSEndpoint` takes a full `ws://user:pass@host:port`) and a
    proxy (`page.authenticate`).

Usage
-----
    python puppeteer_scraper.py --city Goa --pages 3 \\
        --cdp-endpoint "$MAKEMYTRIP_CDP_ENDPOINT"

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from typing import Optional

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job fails on any reported skip.
# That only works if importing this module actually requires the driver
# (CLAUDE.md §10).
from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ProxyError,
                        split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own (§8).
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30

# Chromium's own names for a proxy that could not be used (§8).
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

# pyppeteer's dialect of the fetch() in playwright_scraper.FETCH_JS: the
# arguments arrive POSITIONALLY rather than as one array. Same body, same
# return shape, same AbortController timeout (§8), same `credentials:
# "include"` and the same headers the site's own front end sends.
FETCH_JS = """
async (url, method, body, headers, timeoutMs) => {
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


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Lets this engine drive page_flow's synchronous fetch loop unchanged,
    and gives every call an explicit, enforced timeout: `.result(timeout)`
    returns control even when the browser never answers, which pyppeteer's
    own API does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each as an ERROR after a successful run has printed
        # its results. Only that shape is swallowed; anything else still gets
        # the default handler.
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                # A rejected --cdp-endpoint handshake, raised by websockets in
                # a task pyppeteer never awaits, AFTER the connect has already
                # timed out and been reported with the reason.
                "server rejected WebSocket connection",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING what it still has in flight, on the loop
        thread, so asyncio does not print a traceback per pending task after
        a successful run."""
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


# Every `scheme://user:pass@` in a string, however many times it occurs (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version (§8).
    pyppeteer's `browser.version()` returns "HeadlessChrome/115.0.0.0"."""
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


def _proxy_failure(text) -> str:
    text = str(text)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


class _Ops:
    """One pyppeteer browser + page, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser (§8).
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None
        self.landed = False

    def open(self):
        self.landed = False
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            err = None
            for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
                try:
                    # No `ignoreHTTPSErrors` here. With it, pyppeteer 2.0.0's
                    # connect() never returned from a Scraping Browser
                    # profile (40 s, then the bridge's timeout), and without
                    # it the same profile connected in 1.7 s, twice, in one
                    # session on 2026-09-24. The sibling this engine was
                    # copied from passed it and was measured on an older
                    # pyppeteer. A remote browser's certificates are not
                    # ours to overrule anyway.
                    self.browser = self.bridge.run(
                        connect(browserWSEndpoint=self.args.cdp_endpoint),
                        timeout=page_flow.CDP_CONNECT_TIMEOUT_S)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001 — see below
                    err = e
                    if (attempt < page_flow.CDP_CONNECT_ATTEMPTS
                            and page_flow.cdp_should_retry(str(e))):
                        logger.warning("The Scraping Browser profile did not "
                                       "accept the connection (attempt %d/%d) "
                                       "— it may still be locked by a previous "
                                       "run; retrying in %.0fs.", attempt,
                                       page_flow.CDP_CONNECT_ATTEMPTS,
                                       page_flow.CDP_LOCKED_WAIT_S)
                        time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                        continue
                    break
            if err is not None:
                e = err
                # websockets' message ("server rejected WebSocket connection:
                # HTTP 500") names neither the endpoint nor the reason.
                # Re-raised masked, with the meaning spelled out, so
                # __main__ can map it onto exit 5.
                raise RuntimeError(
                    "could not connect to --cdp-endpoint %s: %s\n%s"
                    % (_mask_credentials(self.args.cdp_endpoint),
                       _mask_credentials(str(e)),
                       page_flow.cdp_connect_hint(str(e)))) from None
            self.page = self.bridge.run(self.browser.newPage())
            self._enable_autosolve()
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       f"--lang={self.args.locale}"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= is part of the browser's argv,
            # readable by anything that can run `ps` (§8).
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))
        # Signal handlers off: pyppeteer installs them inside launch(), and
        # `signal.signal` raises off the main thread, which is where this
        # event loop lives. close() handles teardown instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        if self.args.fingerprint:
            self._apply_fingerprint()
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def _enable_autosolve(self):
        """The Scraping Browser API's own CAPTCHA domain, as the Playwright
        engine enables it: if a captcha ever appears in front of the landing,
        the extension can clear it before the local solver gets a turn. It
        was missing from this engine in a sibling repo until a live run over
        --cdp-endpoint showed the difference (CLAUDE.md §26)."""
        async def _enable():
            # One coroutine for both calls: pyppeteer's CDPSession.send
            # returns a Future rather than a coroutine, and the bridge's
            # run_coroutine_threadsafe accepts only the latter ("A coroutine
            # object is required" on the first live run).
            session = await self.page.target.createCDPSession()
            await session.send("Captcha.setAutoSolve",
                               {"autoSolve": True, "options": [{"type": "*"}]})

        try:
            self.bridge.run(_enable(), timeout=30)
            logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:  # noqa: BLE001 — a non-Scraping-Browser endpoint
            logger.info("Captcha.setAutoSolve not available on this "
                        "--cdp-endpoint (%s) — relying on this script's own "
                        "detect+solve logic instead.", e)

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install, so no engine
        applies a different half of one fingerprint."""
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(
                self.page.evaluateOnNewDocument(playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:  # noqa: BLE001 — a fingerprint is not the run
            logger.warning("Could not apply the fingerprint (%s) — continuing "
                           "without it.", e)

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.bridge.run(self.page.goto(
                url, waitUntil="domcontentloaded", timeout=60000), timeout=90)
        except Exception as e:  # noqa: BLE001 — pyppeteer raises several types
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        if resp is None:
            return None, None
        return resp.status, None

    def document_text(self) -> str:
        try:
            return self.bridge.run(self.page.content()) or ""
        except Exception:  # noqa: BLE001
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self, self.args)

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.bridge.run(self.page.evaluate(
                FETCH_JS, req.url, req.method, req.body_json, req.headers,
                timeout_ms), timeout=timeout_ms / 1000 + 15)
        except Exception as e:  # noqa: BLE001
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser, or a fresh connection to the remote one: after a
        refused connection the Scraping Browser served the reconnect from a
        different Indian exit (2026-09-24)."""
        self.close()
        if self.remote:
            time.sleep(page_flow.CDP_LOCKED_WAIT_S)
        self.open()

    def close(self):
        """Close the page, and on a REMOTE browser disconnect too: closing
        only the page leaves the websocket open, and its unwinding prints
        tracebacks after the output is written. The remote BROWSER is left
        running; it is not ours."""
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
                self.bridge.run(self.browser.disconnect(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def handle_captcha_if_present(ops, args) -> bool:
    """Detect and solve a reCAPTCHA on the current document. Mirrors
    playwright_scraper.handle_captcha_if_present: called only for a landing
    classified as `challenge`, which has not been observed on this site;
    nothing is paid without a widget; a solver error is a warning."""
    try:
        html = ops.bridge.run(ops.page.content())
    except Exception:  # noqa: BLE001
        return False
    html_challenge = detect_recaptcha_v3(html, ops.page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: ops.bridge.run(ops.page.evaluate(js)), page_url=ops.page.url)
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
    ops.bridge.run(ops.page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading the landing.")
    time.sleep(1.5)
    ops.bridge.run(ops.page.reload(waitUntil="domcontentloaded", timeout=60000),
                   timeout=90)
    return True


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    bridge = _AsyncBridge()
    try:
        return page_flow.run_pages(
            lambda: _Ops(bridge, args, pool).open(),
            lambda ops: ops.close(),
            args, pool, args.query, _mask_credentials)
    finally:
        bridge.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="makemytrip.com hotel listing scraper (pyppeteer edition)")
    page_flow.add_arguments(p, engine="puppeteer")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium that runs under "
                        "Rosetta far enough to print --version and then fails "
                        "to open its DevTools socket. Point it at a Chrome or "
                        "Chromium of your own — Playwright's, if installed.")
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
    except RuntimeError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
