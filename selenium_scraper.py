#!/usr/bin/env python3
"""
binance-scraper — Selenium edition (secondary engine)
=====================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The fetch loop that decides all three lives in page_flow.py
and is shared, so this file is browser plumbing and nothing else.

    --mode p2p            (default)  the P2P order book for --asset/--fiat
    --mode copytrading               Futures copy-trading lead portfolios
    --mode announcements             one announcement catalogue

Three limits of this engine, stated here rather than left to be discovered.
None is a bug in this code and none can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.**
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password, so a credentialled --cdp-endpoint (the Scraping
    Browser API) is refused with exit 2 rather than connected to and
    silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=`
    accepts no credentials. They are stripped and a warning says so.
  * **Selenium reports no HTTP status for a navigation.** The landing is
    therefore judged on what the document holds: the endpoint's own JSON
    envelope, or the WAF's markers. A fetch() does report its status, so
    every page after the landing is judged exactly as in the twins.

On this site the first two bite less than elsewhere: the data endpoints
answered a datacentre address normally, so no credentialled exit is needed
for any mode.

Usage
-----
    python selenium_scraper.py --asset USDT --fiat EUR --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse, urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

from captcha_solver import detect_aws_waf, solve_recaptcha, AWS_WAF_COOKIE
from product_parser import (ANN_CATALOGS, COPY_SORTS, COPY_TIME_RANGES,
                            DEFAULT_ANN_CATALOG, DEFAULT_COPY_SORT, P2P_SIDES)
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
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
# Same request, same return shape, same AbortController timeout (§8).
FETCH_JS = """
var done = arguments[arguments.length - 1];
var url = arguments[0], method = arguments[1], body = arguments[2];
var ctl = new AbortController();
var timer = setTimeout(function () { ctl.abort(); }, arguments[3]);
var init = {method: method, credentials: "include", signal: ctl.signal,
            headers: {"accept": "application/json, text/plain, */*"}};
if (body !== null) {
  init.headers["content-type"] = "application/json";
  init.body = body;
}
fetch(url, init).then(function (r) {
  return r.text().then(function (t) {
    clearTimeout(timer);
    done({status: r.status, text: t, waf: r.headers.get("x-amzn-waf-action")});
  });
}).catch(function (e) {
  clearTimeout(timer);
  done({status: 0, text: "", waf: null, error: String(e)});
});
"""

BODY_TEXT_JS = "return document.body ? document.body.innerText : '';"


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


def _proxy_for_task(args, pool) -> Optional[str]:
    """The exit an AmazonTask should solve from. Never a credentialled one
    here, because Selenium could not have used its credentials either."""
    if args.cdp_endpoint or not pool:
        return None
    return pool.current


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
        # No status and no headers from a Selenium navigation: see the
        # module docstring. The classifier reads the document instead.
        return None, None

    def document_text(self) -> str:
        try:
            text = self.driver.execute_script(BODY_TEXT_JS) or ""
        except WebDriverException:
            text = ""
        if text.lstrip().startswith("{"):
            return text
        try:
            return self.driver.page_source or text
        except WebDriverException:
            return text

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self, self.args,
                                         _proxy_for_task(self.args, self.pool))

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.driver.execute_async_script(
                FETCH_JS, req.url, req.method, req.body_json, timeout_ms)
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
        if self.remote:
            self.landed = False
            return
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


def handle_captcha_if_present(ops, args, proxy: Optional[str] = None) -> bool:
    """Solve an AWS WAF CAPTCHA on the current document. Mirrors
    playwright_scraper.handle_captcha_if_present."""
    try:
        html = ops.driver.page_source
        url = ops.driver.current_url
    except WebDriverException:
        return False
    challenge = detect_aws_waf(html, url)
    if challenge is None:
        return False
    if not challenge.has_captcha_widget:
        logger.info("AWS WAF %s action and no CAPTCHA widget on the page — "
                    "not sending it to the solver; there is no puzzle to buy "
                    "an answer to.", challenge.aws_waf_action)
        return False
    logger.warning("AWS WAF CAPTCHA on %s — attempting to solve (AmazonTask%s).",
                   url, "" if proxy else "Proxyless")
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key — cannot solve it. Set "
                       "TWOCAPTCHA_KEY in .env, or use a residential exit "
                       "(--proxy), which the WAF may not challenge at all.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score, proxy=proxy)
    except Exception as e:  # noqa: BLE001 — a solver error is a warning (§8)
        logger.error("Solving the AWS WAF CAPTCHA failed (%s) — continuing.",
                     _mask_credentials(str(e)))
        return False
    domain = page_flow.cookie_domain(urlparse(url).hostname, html)
    try:
        ops.driver.add_cookie({"name": AWS_WAF_COOKIE, "value": token,
                               "domain": domain, "path": "/"})
        logger.info("Set %s for %s — reloading to let the WAF re-check.",
                    AWS_WAF_COOKIE, domain)
        time.sleep(1.5)
        ops.driver.refresh()
    except WebDriverException as e:
        logger.error("Could not apply the solved token (%s).",
                     _mask_credentials(str(e)))
        return False
    return True


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers, each with its own
    driver and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            ops = _Ops(args, page_flow.worker_pool(pool, index)).open()
            try:
                page_flow.worker_loop(ops, args, query, work, results,
                                      results_lock, exhausted, name,
                                      _mask_credentials)
            finally:
                ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    concurrency = page_flow.concurrency_for(args, pool)
    return page_flow.run_pages(
        lambda: _Ops(args, pool).open(),
        lambda ops: ops.close(),
        lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                pages, concurrency),
        args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="binance.com scraper — P2P adverts, copy-trading lead "
                    "portfolios and announcements (Selenium edition)")
    p.add_argument("--mode", choices=["p2p", "copytrading", "announcements"],
                   default=None,
                   help="p2p (default): the P2P order book for --asset/--fiat. "
                        "copytrading: Futures copy-trading lead portfolios. "
                        "announcements: one announcement catalogue, newest "
                        "first. Inferred from --url when that is given.")
    p.add_argument("--url", default=None,
                   help="A binance.com page to read the query from instead of "
                        "the flags: a P2P trade page "
                        "(p2p.binance.com/en/trade/all-payments/USDT?fiat=EUR, "
                        "…/trade/sell/BTC?fiat=TRY), /en/copy-trading, or "
                        "/en/support/announcement/list/{id}. The page itself "
                        "is never fetched — it is behind AWS WAF — only the "
                        "query in it is read. Also read from BINANCE_URL.")
    g = p.add_argument_group("p2p")
    g.add_argument("--asset", default=None,
                   help="The crypto asset (default USDT).")
    g.add_argument("--fiat", default=None,
                   help="The fiat currency, ISO 4217 (default USD).")
    g.add_argument("--side", choices=P2P_SIDES, default=None,
                   help="buy (default): adverts you could BUY the asset from. "
                        "Those adverts are marked SELL by the site, because "
                        "an advert carries the maker's side; the row keeps "
                        "both as `side` and `advertiser_side`.")
    g.add_argument("--pay-type", action="append", default=None, metavar="ID",
                   help="Only adverts taking this payment method, by the "
                        "site's identifier (SEPAinstant, Wise, BANK, …). "
                        "Repeatable. Checked against the site's own list for "
                        "the fiat before the search runs: the search answers "
                        "an unknown one with an EMPTY result, not an error.")
    g.add_argument("--amount", type=float, default=None,
                   help="Only adverts whose limits admit an order of this much "
                        "fiat.")
    g = p.add_argument_group("copytrading")
    g.add_argument("--time-range", choices=COPY_TIME_RANGES, default=None,
                   help="The period ROI, PnL and drawdown cover (default 30D).")
    g.add_argument("--sort-by", choices=sorted(COPY_SORTS), default=None,
                   help="Ordering (default %s). Not cosmetic: a capped run "
                        "holds the first N portfolios by this key, so it "
                        "decides WHICH portfolios are in the file. `sharpe` "
                        "also filters: portfolios without a Sharpe ratio are "
                        "left out. Win rate is not offered: the API gave a "
                        "nonsense key the same answer." % DEFAULT_COPY_SORT)
    g.add_argument("--order", choices=["desc", "asc"], default=None,
                   help="desc (default) or asc.")
    g.add_argument("--hide-full", action="store_true",
                   help="Leave out portfolios with no copier seat free.")
    g = p.add_argument_group("announcements")
    g.add_argument("--category", default=None,
                   help="The announcement catalogue: %s, or a numeric "
                        "catalogue id (default %s)."
                        % (", ".join(ANN_CATALOGS), DEFAULT_ANN_CATALOG))
    p.add_argument("--pages", type=int, default=1,
                   help="Pages to fetch (20 adverts, 30 portfolios or 50 "
                        "announcements each). Planned against the total the "
                        "site states on page 1, so asking for more than exist "
                        "fetches all of them.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default %(default)s)")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Each worker runs its own browser and holds its own "
                        "proxy exit. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page on a transport failure (default 3). "
                        "The pause doubles each time. A request the endpoint "
                        "REFUSED is not retried: its parameters would be "
                        "refused again.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="binance_rows", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It changes nothing in "
                        "the data: the endpoints answer in English whatever "
                        "the browser claims.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh browser, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page is refused (403, 451, AWS WAF), retry it "
                        "from this many OTHER exits (default 2). Needs a pool "
                        "of more than one.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a browser fingerprint from 2captcha's "
                        "Fingerprint API. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use (v2: createTask, "
                        "which AmazonTask needs).")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): solve an AWS WAF CAPTCHA only "
                        "when it stands between the run and the data. No data "
                        "path here renders one to an unchallenged session, so "
                        "'always' behaves the same.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9). Kept "
                        "for parity with the family; AWS WAF has no score.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching Chromium, e.g. the Scraping "
                        "Browser API endpoint ws://user:pass@host:port. "
                        "--proxy and --headless/--headful are ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact response the parser is given, on "
                        "success as well as failure. It is JSON; the flag "
                        "keeps the family's name.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args(argv)
    env_config.apply(args)
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
