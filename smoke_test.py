#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for binance-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They are real API
responses captured 2026-09-24, trimmed and scrubbed by `make_fixtures.py`,
which proves each one parses identically to its untrimmed original. Not
verbatim: advertiser and lead nicknames, advertiser ids, avatar URLs and
announcement codes are placeholders (see make_fixtures.py for why each).
"""

import argparse
import ast
import copy
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


FIXTURES_PATH = os.path.join(HERE, "fixtures_generated.json")
FIXTURES = json.load(open(FIXTURES_PATH, encoding="utf-8"))


def fx(name) -> str:
    """A fixture as the text an endpoint returns."""
    value = FIXTURES[name]
    return value if isinstance(value, str) else json.dumps(value)


ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {"playwright_scraper": "playwright",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    expected = {"p2p_usdt_eur_buy", "p2p_btc_try_sell", "p2p_past_the_end",
                "p2p_illegal_rows", "p2p_pay_types_eur", "copy_30d_roi",
                "copy_7d_pnl", "copy_past_the_end", "ann_new_listings",
                "ann_delisting", "ann_past_the_end", "waf_captcha_chromium",
                "cdp_extension_injection"}
    missing = expected - set(FIXTURES)
    check("every fixture the suite uses is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 20000, "%d bytes" % len(blob))
    check("no 32-hex string survived the scrub",
          not re.search(r"\b[0-9a-f]{32}\b", blob))
    check("the P2P nicknames are placeholders",
          "fixture-advertiser-1" in blob and "BURGENK" not in blob)
    check("the announcement codes are placeholders",
          "fixture-code-1" in blob)


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage (§10)
# ---------------------------------------------------------------------------

def _q(mode, **kw):
    from product_parser import Query
    return Query(mode=mode, **kw)


def check_p2p_buy_parses_to_the_captured_values():
    import product_parser as P
    q = _q("p2p", asset="USDT", fiat="EUR", side="buy")
    rows = P.parse_page(fx("p2p_usdt_eur_buy"), q, 1)
    equal("4 kept adverts become 4 rows", len(rows), 4)
    r = rows[0]
    equal("price is a FLOAT parsed from the endpoint's string '0.869'", r.price, 0.869)
    check("price is a float, not the string", isinstance(r.price, float))
    equal("sku is advNo, kept as a string (past 2**53)", r.sku, "12935358234995494912")
    equal("fiat", r.fiat, "EUR")
    equal("asset", r.asset, "USDT")
    equal("the per-order limits", (r.min_order_fiat, r.max_order_fiat), (100.0, 105.0))
    equal("available quantity", r.available, 184.24)
    equal("payment methods are a LIST of identifiers", r.pay_methods, ["Wise"])
    equal("the time limit", r.pay_time_limit_min, 15)
    equal("month orders", r.month_orders, 37)
    equal("positive rate is a fraction", r.positive_rate, 0.6826923)
    equal("privilegeType is written through uninterpreted", r.privilege_type, 1)
    equal("the advertiser's public profile URL",
          r.url, "https://p2p.binance.com/en/advertiserDetail?advertiserNo=sFIXTURE-ADVERTISER-1")
    equal("positions are 1..4", [x.position for x in rows], [1, 2, 3, 4])


def check_p2p_side_is_inverted_in_the_data_and_both_are_kept():
    """A buy query returns adverts marked SELL: an advert carries the maker's
    side. 20 of 20 on the captured page, and 20 of 20 the other way round on a
    sell query. A consumer reading tradeType alone gets every row backwards."""
    import product_parser as P
    buy = P.parse_page(fx("p2p_usdt_eur_buy"), _q("p2p", fiat="EUR", side="buy"))
    sell = P.parse_page(fx("p2p_btc_try_sell"),
                        _q("p2p", asset="BTC", fiat="TRY", side="sell"))
    equal("buy query: side is buy on every row", {r.side for r in buy}, {"buy"})
    equal("...and the adverts say sell", {r.advertiser_side for r in buy}, {"sell"})
    equal("sell query: side is sell", {r.side for r in sell}, {"sell"})
    equal("...and the adverts say buy", {r.advertiser_side for r in sell}, {"buy"})
    equal("a TRY price in the millions parses", sell[0].price, 4105272.0)
    check("the request carries the TAKER's side",
          P.request_for(_q("p2p", side="buy"), 1).body["tradeType"] == "BUY")


def check_copytrading_parses_to_the_captured_values():
    import product_parser as P
    rows = P.parse_page(fx("copy_30d_roi"), _q("copytrading"), 1)
    equal("3 kept portfolios", len(rows), 3)
    r = rows[0]
    equal("sku is leadPortfolioId", r.sku, "5219911688676500225")
    equal("ROI is a percentage as published", r.roi_pct, 4557.3715)
    equal("PnL", r.pnl, 22786.8575)
    equal("drawdown", r.mdd_pct, 1.5226)
    equal("win rate", r.win_rate_pct, 80.8219)
    equal("copiers / seats", (r.copiers, r.max_copiers), (399, 400))
    equal("a portfolio with a seat left is not full", r.is_full, False)
    equal("badge", r.badge, "MASTER")
    equal("API trading is read from apiKeyTag", r.api_trading, True)
    equal("TradFi is read from tradFiTag", r.tradfi, True)
    equal("a null Sharpe ratio stays null, not 0", r.sharpe_ratio, None)
    equal("started_at from epoch ms", r.started_at, "2026-09-11T01:16:22.096000+00:00")
    equal("the period is on the row", r.time_range, "30D")
    equal("the ORDERING is on the row", r.sort, "roi-desc")
    equal("the lead page URL",
          r.url, "https://www.binance.com/en/copy-trading/lead-details/5219911688676500225")
    pnl = P.parse_page(fx("copy_7d_pnl"),
                       _q("copytrading", time_range="7D", sort_by="pnl"), 1)
    equal("a 7D-by-PnL run says so on every row",
          {(x.time_range, x.sort) for x in pnl}, {("7D", "pnl-desc")})


def check_announcements_parse_to_the_captured_values():
    import product_parser as P
    rows = P.parse_page(fx("ann_new_listings"), _q("announcements"), 1)
    equal("3 kept articles", len(rows), 3)
    r = rows[0]
    equal("sku is the numeric article id", r.sku, "285492")
    equal("title", r.title, "Binance Will List Hyperliquid (HYPE) with Seed Tag Applied")
    equal("catalogue", (r.catalog_id, r.catalog_name), (48, "New Cryptocurrency Listing"))
    equal("released_at from epoch ms", r.released_at, "2026-09-24T07:30:38.608000+00:00")
    equal("the canonical /detail/{code} address (what a slug link redirects to)",
          r.url, "https://www.binance.com/en/support/announcement/detail/fixture-code-1")
    d = P.parse_page(fx("ann_delisting"), _q("announcements", catalog="delisting"), 1)
    equal("the delisting catalogue", {x.catalog_id for x in d}, {161})


def check_totals_are_read_from_page_one_and_planned():
    import product_parser as P
    equal("P2P total", P.total_results(fx("p2p_usdt_eur_buy"), "p2p"), 187)
    equal("...is 10 pages of 20", P.pages_available(187, "p2p"), 10)
    equal("copy-trading total", P.total_results(fx("copy_30d_roi"), "copytrading"), 8921)
    equal("...is 298 pages of 30 (the silent cap)", P.pages_available(8921, "copytrading"), 298)
    equal("announcements total",
          P.total_results(fx("ann_new_listings"), "announcements"), 2269)
    equal("...is 46 pages of 50", P.pages_available(2269, "announcements"), 46)
    equal("P2P reports total 0 on a page PAST the end, which is why only "
          "page 1's total is read",
          P.total_results(fx("p2p_past_the_end"), "p2p"), 0)
    import page_flow
    equal("asking for more pages than exist plans what exists",
          page_flow.pages_to_plan(50, 10), 10)
    equal("...and never fewer than one", page_flow.pages_to_plan(3, 0), 1)
    equal("an unknown total falls back to the cap",
          page_flow.pages_to_plan(5, None), 5)


def check_page_sizes_are_the_measured_ones():
    """Each of these is a measurement, and each is a trap when wrong."""
    import product_parser as P
    equal("P2P rows: 20 (50 is 'illegal parameter')", P.P2P_ROWS, 20)
    equal("copy-trading: 30, the size the server silently caps at", P.COPY_ROWS, 30)
    check("announcements: a size from the accepted set {1,2,5,10,15,20,50}",
          P.ANN_ROWS in (1, 2, 5, 10, 15, 20, 50), repr(P.ANN_ROWS))
    equal("the request uses those sizes",
          (P.request_for(_q("p2p"), 3).body["rows"],
           P.request_for(_q("copytrading"), 3).body["pageSize"],
           P.request_for(_q("announcements"), 3).params["pageSize"]),
          (20, 30, 50))
    equal("and the page number",
          (P.request_for(_q("p2p"), 3).body["page"],
           P.request_for(_q("copytrading"), 3).body["pageNumber"],
           P.request_for(_q("announcements"), 3).params["pageNo"]),
          (3, 3, 3))
    equal("P2P and copy-trading are POST, announcements GET",
          [P.request_for(_q(m), 1).method for m in ("p2p", "copytrading", "announcements")],
          ["POST", "POST", "GET"])


def check_position_counts_emitted_rows_not_payload_slots():
    """§24: a record the parser drops must not shift every later position."""
    import product_parser as P
    payload = copy.deepcopy(FIXTURES["p2p_usdt_eur_buy"])
    payload["data"].insert(1, {"adv": None, "advertiser": {}})
    rows = P.parse_page(json.dumps(payload), _q("p2p", fiat="EUR"), 1)
    equal("the malformed record is dropped", len(rows), 4)
    equal("...and positions stay contiguous", [r.position for r in rows], [1, 2, 3, 4])


def check_page_and_position_are_unique_across_pages():
    import product_parser as P
    q = _q("copytrading")
    p1 = P.parse_page(fx("copy_30d_roi"), q, 1)
    p2 = P.parse_page(fx("copy_30d_roi"), q, 2)
    pairs = [(r.page, r.position) for r in p1 + p2]
    equal("page+position is unique across a multi-page run", len(set(pairs)), len(pairs))
    equal("page 2's rows really say page 2", {r.page for r in p2}, {2})


def check_values_the_site_did_not_state_stay_null():
    import product_parser as P
    equal("an implausible timestamp is None, not 1970", P._iso_ms(5), None)
    equal("a non-numeric price is None", P._float("n/a"), None)
    equal("a boolean is not a number", P._float(True), None)
    equal("an absent string is None, not ''", P._str("  "), None)


# ---------------------------------------------------------------------------
# The query: every parameter allowlisted, because the API does not validate
# ---------------------------------------------------------------------------

def check_the_api_silent_fallbacks_are_refused_up_front():
    """Measured: an unknown dataType returns a full list under SOME ordering,
    a pageSize above 30 is silently capped, and an unknown payTypes entry
    returns an empty list. Each is a typo that looks like a healthy run."""
    import product_parser as P
    check("win rate is NOT offered: the API gave a nonsense key the same answer",
          "win-rate" not in P.COPY_SORTS and "WIN_RATE" not in P.COPY_SORTS.values())
    check("an unknown ordering is refused",
          _q("copytrading", sort_by="winrate").validate() is not None)
    check("1Y is refused (the API answers 11012004)",
          _q("copytrading", time_range="1Y").validate() is not None)
    check("365D is accepted (measured)", _q("copytrading", time_range="365D").validate() is None)
    check("an unknown side is refused", _q("p2p", side="short").validate() is not None)
    check("a fiat that is not 3 letters is refused", _q("p2p", fiat="EURO").validate() is not None)
    check("an unknown catalogue alias is refused",
          _q("announcements", catalog="listings").validate() is not None)
    check("a numeric catalogue id is accepted",
          _q("announcements", catalog="161").validate() is None)
    equal("aliases map to the site's own ids",
          [P.catalog_id(a) for a in ("new-listings", "delisting", "news")], [48, 161, 49])


def check_pay_types_are_checked_against_the_sites_list():
    import product_parser as P
    offered = P.pay_type_identifiers(fx("p2p_pay_types_eur"))
    check("the site's list for EUR is read", offered and "SEPAinstant" in offered
          and "Wise" in offered, repr(offered))
    equal("an exact identifier passes", P.check_pay_types(["Wise"], offered), None)
    msg = P.check_pay_types(["SEPA Instant"], offered) or ""
    check("the page's display name is refused...", "SEPA Instant" in msg)
    check("...and the real identifier is suggested", "SEPAinstant" in msg, msg)
    equal("an unreadable list does not refuse a correct run",
          P.check_pay_types(["Wise"], None), None)
    equal("a response without the shape reads as unreadable, not as []",
          P.pay_type_identifiers('{"code":"000000","data":null}'), None)


def check_url_shapes():
    import product_parser as P
    q, why = P.query_from_url("https://p2p.binance.com/en/trade/all-payments/USDT?fiat=EUR")
    equal("a P2P buy page", (q.mode, q.side, q.asset, q.fiat, q.pay_types),
          ("p2p", "buy", "USDT", "EUR", ()))
    q, _ = P.query_from_url("https://p2p.binance.com/en/trade/Wise/USDT?fiat=EUR")
    equal("a payment in the buy path is a --pay-type", q.pay_types, ("Wise",))
    q, _ = P.query_from_url("https://p2p.binance.com/en/trade/sell/BTC?fiat=TRY&payment=Papara")
    equal("a P2P sell page", (q.side, q.asset, q.fiat, q.pay_types),
          ("sell", "BTC", "TRY", ("Papara",)))
    q, _ = P.query_from_url("https://www.binance.com/en/copy-trading")
    equal("the copy-trading page", q.mode, "copytrading")
    q, _ = P.query_from_url("https://www.binance.com/en/support/announcement/list/161")
    equal("an announcement catalogue", (q.mode, q.catalog), ("announcements", "161"))
    q, why = P.query_from_url("https://www.binance.us/en/markets")
    check("binance.us is refused WITH the reason", q is None and "binance.us" in why, why)
    q, why = P.query_from_url("https://www.binance.com/en/markets/overview")
    check("an unsupported page is refused, naming what IS supported",
          q is None and "copy-trading" in why, why)


def check_url_and_flags_together_are_refused():
    import page_flow
    errors = []

    def err(msg):
        errors.append(msg)
        raise SystemExit(2)

    args = types.SimpleNamespace(
        url="https://p2p.binance.com/en/trade/all-payments/USDT?fiat=EUR",
        mode=None, asset=None, fiat="TRY", side=None, pay_type=None,
        amount=None, time_range=None, sort_by=None, order=None,
        hide_full=False, category=None, pages=1)
    try:
        page_flow.build_query(args, err)
    except SystemExit:
        pass
    check("--url with --fiat is refused, not merged",
          errors and "--fiat" in errors[0], repr(errors))
    args.fiat = None
    errors.clear()
    q = page_flow.build_query(args, err)
    equal("--url alone reads the query from the address", (q.mode, q.fiat), ("p2p", "EUR"))
    args.url, args.mode = None, "copytrading"
    q = page_flow.build_query(args, err)
    equal("flag defaults apply without --url", (q.time_range, q.sort_by, q.order),
          ("30D", "roi", "desc"))


# ---------------------------------------------------------------------------
# Page state: what the site answered with
# ---------------------------------------------------------------------------

def check_page_states_on_real_captures():
    import page_flow as F
    equal("a listing with rows is content", F.classify(fx("p2p_usdt_eur_buy"), 200), "content")
    equal("a page past the end is EMPTY, an answer", F.classify(fx("p2p_past_the_end"), 200), "empty")
    equal("...on copy-trading too", F.classify(fx("copy_past_the_end"), 200), "empty")
    equal("...and on announcements", F.classify(fx("ann_past_the_end"), 200), "empty")
    equal("a non-success code is REJECTED, not blocked",
          F.classify(fx("p2p_illegal_rows"), 200), "rejected")
    import product_parser as P
    equal("...and the site's complaint is named",
          P.api_error(fx("p2p_illegal_rows")), "code 000002: illegal parameter")
    equal("HTTP 400 with an EMPTY body (a bad pageSize) is rejected", F.classify("", 400), "rejected")
    equal("AWS WAF's 202 challenge, empty body + header", F.classify("", 202, "", "challenge"), "challenge")
    equal("...and without the header, on status alone", F.classify("", 202), "challenge")
    equal("AWS WAF's CAPTCHA page as real Chromium got it",
          F.classify(fx("waf_captcha_chromium"), 405), "challenge")
    equal("...recognised by its markers even with no status (Selenium)",
          F.classify(fx("waf_captcha_chromium"), None), "challenge")
    equal("429 is throttled", F.classify("", 429), "throttled")
    equal("418 (the site's ban after 429) is throttled", F.classify("", 418), "throttled")
    equal("451 is restricted: the exit's COUNTRY", F.classify("", 451), "restricted")
    equal("403 is blocked", F.classify("<html>403 Forbidden</html>", 403), "blocked")
    equal("anything else is unknown", F.classify("<html>hello</html>", 200), "unknown")


def check_the_waf_page_is_solvable_and_detected_fully():
    from captcha_solver import detect_aws_waf
    c = detect_aws_waf(fx("waf_captcha_chromium"),
                       "https://www.binance.com/en/support/announcement")
    check("the captured CAPTCHA page is detected as AWS WAF", c is not None and c.is_aws_waf)
    check("...with a widget, so a solve has something to buy", c and c.has_captcha_widget)
    check("...and every AmazonTask field present",
          c and all([c.sitekey, c.iv, c.context, c.challenge_script, c.captcha_script]))


def check_markers_do_not_match_a_page_the_scraping_browser_served():
    """§24: the Scraping Browser's auto-solve extension injects captcha
    hunters into EVERY page, amazon_waf among them. The marker set must score
    zero against that injection WITHOUT any strip, or the strip is
    load-bearing and the next marker inherits the hole."""
    import product_parser as P
    html = fx("cdp_extension_injection")
    check("the fixture really carries the amazon_waf hunter (not vacuous)",
          "amazon_waf" in html and "turnstile" in html)
    equal("no AWS WAF marker fires on the extension's injection",
          P.detect_bot_challenge(html), None)
    for name in ("p2p_usdt_eur_buy", "copy_30d_roi", "ann_new_listings"):
        equal("no marker fires on served data (%s)" % name,
              P.detect_bot_challenge(fx(name)), None)
    check("the bare word 'captcha' is not a marker",
          all(m.lower() != "captcha" for m in P.AWS_WAF_MARKERS))


def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "rejected",
           "restricted", "throttled", "unknown"])
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("rejected: not retried, not solved, NOT blocked (a typo is not a proxy problem)",
          not F.should_retry("rejected") and not F.should_solve("rejected")
          and not F.counts_as_blocked("rejected") and not F.should_parse("rejected"))
    check("challenge: retried, solved, blocked",
          F.should_retry("challenge") and F.should_solve("challenge")
          and F.counts_as_blocked("challenge"))
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    check("restricted and blocked: never solved, there is no widget",
          not F.should_solve("restricted") and not F.should_solve("blocked"))
    equal("at most one solve per page", F.SOLVES_PER_PAGE, 1)
    equal("refusals are reported by name",
          [F.refusal_name(s) for s in ("challenge", "restricted", "blocked")],
          ["aws-waf", "geo-451", "http-403"])
    check("451's advice names the country, not a solver",
          "COUNTRY" in F.refusal_advice("restricted"))
    check("...and claims nothing about what binance.com has been SEEN doing",
          "answers" not in F.refusal_advice("restricted"))
    check("a CDP 401 is explained as expired credentials, not a held pid",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized")
          and "pid" not in F.cdp_connect_hint("401 Unauthorized"))
    check("...and a 500 as a held pid", "pid" in F.cdp_connect_hint("HTTP 500"))
    waf = fx("waf_captcha_chromium")
    check("the captured CAPTCHA page names its cookie domains (not vacuous)",
          "awsWafCookieDomainList" in waf)
    equal("the aws-waf-token goes on the domain the SITE lists",
          (F.cookie_domain("www.binance.com", waf), F.cookie_domain("p2p.binance.com", waf)),
          (".binance.com", ".binance.com"))
    equal("an EMPTY list means the page host (transfermarkt's, 2026-09-24)",
          F.cookie_domain("www.transfermarkt.com", "awsWafCookieDomainList = [];"),
          "www.transfermarkt.com")
    equal("a host the list does not cover gets the host",
          F.cookie_domain("www.example.org", waf), "www.example.org")


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    src = open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read()
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE", "THROTTLE_RETRIES", "THROTTLE_WAIT_S",
                     "CHALLENGE_SETTLE_MS", "CHALLENGE_POLL_MS", "FETCH_TIMEOUT_MS",
                     "CORE_FIELD_FLOOR"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        engines = "".join(open(os.path.join(HERE, m + ".py"), encoding="utf-8").read()
                          for m in ENGINES)
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------

class _FakeOps:
    """page_flow's named operations, answering from fixtures."""

    def __init__(self, answers, landing=None):
        self.answers = answers          # page -> list of (status, text, waf)
        self.landing = landing or fx("ann_past_the_end")
        self.landed = False
        self.pool = None
        self.gotos = self.relaunches = self.solves = 0
        self.fetches = []

    def goto(self, url):
        self.gotos += 1
        return 200, None

    def document_text(self):
        return self.landing

    def wait_ms(self, ms):
        pass

    def solve_captcha(self):
        self.solves += 1
        return False

    def fetch(self, req):
        self.fetches.append(req.page)
        if req.page == 0:              # the pay-type list
            return 200, fx("p2p_pay_types_eur"), None, None
        queue = self.answers.get(req.page) or [(200, fx("p2p_past_the_end"), None)]
        status, text, waf = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, text, waf, None

    def relaunch(self):
        self.relaunches += 1
        self.landed = False

    def proxy_failure(self, text):
        return ""

    def close(self):
        pass


def _p2p_page(n):
    """Page n of a P2P listing: the captured page with page-unique ids."""
    payload = copy.deepcopy(FIXTURES["p2p_usdt_eur_buy"])
    for rec in payload["data"]:
        rec["adv"]["advNo"] = "%s%d" % (rec["adv"]["advNo"][:-2], n)
    payload["total"] = 12   # three pages of 4 ... planned as ceil(12/20) = 1
    return json.dumps(payload)


def _run(answers, pages=3, query=None, landing=None, **extra):
    import page_flow
    from product_parser import Query
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=1)
        for k, v in extra.items():
            setattr(args, k, v)
        ops = _FakeOps(answers, landing)
        q = query or Query("p2p", fiat="EUR")
        rc = page_flow.run_pages(lambda: ops, lambda o: None,
                                 lambda pages: ([], [], False), args, None, q, 1)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = (json.load(open(args.out + ".json"))
                if os.path.exists(args.out + ".json") else None)
    return rc, meta, rows, ops


def check_the_shared_loop_end_to_end():
    import product_parser as P
    ann_q = P.Query("announcements")
    rc, meta, rows, ops = _run({1: [(200, fx("ann_new_listings"), None)]},
                               pages=1, query=ann_q)
    equal("a served page: exit 0", rc, 0)
    equal("...complete", meta and meta["status"], "complete")
    equal("...the site's total in the sidecar", meta and meta["total_results"], 2269)
    equal("...and the query too", meta and meta["query"], {"catalog": "new-listings"})
    equal("...rows written", len(rows or []), 3)
    equal("the browser landed ONCE for the run", ops.gotos, 1)

    rc, meta, rows, ops = _run({1: [(200, fx("p2p_illegal_rows"), None)]})
    equal("a REJECTED page 1: exit 5, the data never arrived", rc, 5)
    equal("...fetched once, not retried", ops.fetches, [1])
    equal("...and no sidecar beside no output", meta, None)

    rc, meta, rows, ops = _run({1: [(403, "<html>403 Forbidden</html>", None)]})
    equal("a 403 on page 1: exit 3", rc, 3)
    equal("...re-fetched once from a fresh browser (no pool)", ops.relaunches, 1)

    rc, meta, rows, ops = _run({1: [(451, "", None)]})
    equal("a 451 on page 1: exit 3", rc, 3)

    rc, meta, rows, ops = _run({1: [(200, fx("p2p_past_the_end"), None)]})
    equal("an EMPTY listing: exit 4, and nothing written", (rc, rows), (4, None))

    rc, meta, rows, ops = _run({1: [(429, "", None), (200, _p2p_page(1), None)]}, pages=1)
    equal("a throttle, then the page: exit 0", rc, 0)
    equal("...at the SAME exit (no relaunch)", ops.relaunches, 0)
    equal("...fetched twice", ops.fetches, [1, 1])
    rc, meta, rows, ops = _run({1: [(429, "", None), (200, _p2p_page(1), None)]},
                               pages=1, retries=1)
    equal("a throttle wait spends its OWN budget, not --retries (§24): "
          "with --retries 1 the page still arrives", rc, 0)

    rc, meta, rows, ops = _run({1: [(202, "", "challenge"), (200, _p2p_page(1), None)]}, pages=1)
    equal("a WAF challenge on a fetch, then the page: exit 0", rc, 0)
    equal("...the session LANDED AGAIN before retrying", ops.gotos, 2)

    rc, meta, rows, ops = _run({}, query=P.Query("p2p", fiat="EUR", pay_types=("SEPA Instant",)))
    equal("an unknown --pay-type: exit 2 before any search", rc, 2)
    equal("...no search page was fetched", [p for p in ops.fetches if p != 0], [])


def check_a_multi_page_run_merges_in_page_order_and_ends_on_data():
    import product_parser as P
    q = P.Query("copytrading")
    page2 = copy.deepcopy(FIXTURES["copy_30d_roi"])
    for i, rec in enumerate(page2["data"]["list"]):
        rec["leadPortfolioId"] = "90000000000000000%02d" % i
    answers = {1: [(200, fx("copy_30d_roi"), None)],
               2: [(200, json.dumps(page2), None)],
               3: [(200, fx("copy_past_the_end"), None)]}
    rc, meta, rows, ops = _run(answers, pages=5, query=q)
    equal("an empty page 3 of a planned 5 ends the run: exit 0", rc, 0)
    equal("...as a complete run", meta["status"], "complete")
    equal("...stopped on the DATA", meta["stop_reason"], "end_of_listing")
    equal("...pages 1-3 fetched, not 4-5", ops.fetches, [1, 2, 3])
    equal("rows are in page order", [r["page"] for r in rows], [1, 1, 1, 2, 2, 2])
    dup = {1: [(200, fx("copy_30d_roi"), None)], 2: [(200, fx("copy_30d_roi"), None)],
           3: [(200, fx("copy_past_the_end"), None)]}
    rc, meta, rows, ops = _run(dup, pages=3, query=q)
    equal("a row seen twice across pages (a live listing moving) is kept once",
          len(rows), 3)


def check_every_engine_implements_the_operations_page_flow_uses():
    """The fetch loop is shared, so an engine missing ONE operation fails
    only when a live run reaches it. The set is DERIVED from page_flow's own
    source (every `ops.<name>`), not listed by hand."""
    tree = ast.parse(open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read())
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ops"}
    check("page_flow drives the engines through named operations (not vacuous)",
          {"goto", "fetch", "document_text", "solve_captcha", "relaunch"} <= used,
          repr(sorted(used)))
    check("...and the fake driver this suite uses implements every one",
          all(hasattr(_FakeOps({}), name) for name in used),
          repr(sorted(n for n in used if not hasattr(_FakeOps({}), n))))
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        ops_cls = next((n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "_Ops"), None)
        if ops_cls is None:
            check("%s defines _Ops" % module, False)
            continue
        methods = {n.name for n in ops_cls.body if isinstance(n, ast.FunctionDef)}
        # Targets can be tuples (`self.args, self.pool = args, pool`), so walk
        # into each target rather than reading it whole.
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import (Announcement, LeadTrader, P2PAd,
                               ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES)
    for cls in (P2PAd, LeadTrader, Announcement):
        names = [f.name for f in fields(cls)]
        equal("%s: the family prefix is byte-identical and in order (§9)" % cls.__name__,
              names[:5], ["source", "scraped_at", "url", "sku", "title"])
        check("%s: the run-describing tail is present" % cls.__name__,
              {"page", "position", "mode", "data_source"} <= set(names))
        check("%s: no commerce column that would be null forever" % cls.__name__,
              not ({"currency", "brand", "original_price", "discount_pct"} & set(names)))
    equal("every mode maps to its row class", sorted(ROW_CLASS_BY_MODE),
          ["announcements", "copytrading", "p2p"])
    equal("every mode is one row per sku", sorted(UNIQUE_BY_SKU_MODES),
          ["announcements", "copytrading", "p2p"])
    check("P2P keeps BOTH sides", {"side", "advertiser_side"} <= {f.name for f in fields(P2PAd)})
    check("copy-trading carries its ordering and period",
          {"sort", "time_range"} <= {f.name for f in fields(LeadTrader)})


def check_csv_and_json_writers():
    from output_writer import P2PAd, write_csv, write_json
    import product_parser as P
    rows = P.parse_page(fx("p2p_usdt_eur_buy"), _q("p2p", fiat="EUR"), 1)
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=P2PAd)
        reader = list(csv.reader(open(csv_path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0],
              [f.name for f in fields(P2PAd)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("[") for row in reader[1:] for cell in row))
        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=P2PAd)
        equal("an EMPTY csv still carries its header",
              len(list(csv.reader(open(empty_csv, encoding="utf-8")))), 1)
        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        check("a list column stays a real list in JSON", isinstance(loaded[0]["pay_methods"], list))
        check("an id past 2**53 stays a string in JSON", isinstance(loaded[0]["sku"], str))


def check_exit_codes():
    import output_writer as O
    equal("3 blocked / 4 empty / 5 never obtained / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_FETCH_FAILED, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("end_of_listing is a COMPLETE stop reason (§24)",
          "end_of_listing" in O.COMPLETE_STOP_REASONS)
    check("api_rejected is NOT complete", "api_rejected" not in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        equal("an empty run exits 4", save([], prefix, "json", allow_empty=False), 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(), '[{"sku": "yesterday"}]')
        equal("--allow-empty writes it, and still reports exit 4",
              save([], prefix, "json", allow_empty=True), 4)


def check_diff_runs_tracks_the_real_columns():
    """The sibling family's diff reported 0 changed on real changes for weeks
    because its column list was copied from a repo with different rows. Here
    the list is derived; assert it is never empty and that a real change is
    seen."""
    import diff_runs as D
    for mode in ("p2p", "copytrading", "announcements"):
        check("%s: tracked columns are derived and non-empty" % mode,
              len(D.tracked_fields(mode)) >= 3, repr(D.tracked_fields(mode)))
    check("price is tracked on P2P", "price" in D.tracked_fields("p2p"))
    check("position is NOT tracked (a live listing reorders itself)",
          "position" not in D.tracked_fields("copytrading"))
    import product_parser as P
    old = [asdict(r) for r in P.parse_page(fx("p2p_usdt_eur_buy"), _q("p2p", fiat="EUR"))]
    new = copy.deepcopy(old)
    new[0]["price"] = 0.9
    del new[1]
    result = D.diff_products(old, new)
    equal("one changed", [c["sku"] for c in result["changed"]], [old[0]["sku"]])
    equal("...with the price named", list(result["changed"][0]["changes"]), ["price"])
    equal("one removed", len(result["removed"]), 1)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump([asdict(r) for r in P.parse_page(fx("copy_30d_roi"), _q("copytrading"))],
                  open(b, "w"))
        json.dump({"status": "complete", "query": {"x": 1}}, open(a[:-5] + ".meta.json", "w"))
        json.dump({"status": "complete", "query": {"x": 2}}, open(b[:-5] + ".meta.json", "w"))
        args = types.SimpleNamespace(old=a, new=b)
        check("two different MODES are refused", not D._check_comparable(args))


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="completed", pages_requested=3,
                    pages_completed=3, pages_failed=[], products=90,
                    mode="copytrading", source="binance.com",
                    start_url="https://www.binance.com/x", final_url="https://www.binance.com/y",
                    extra={"total_results": 8921, "pages_available": 298,
                           "query": {"time_range": "30D"}})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source", "total_results", "query"):
        check("the sidecar records %r" % key, key in meta)
    check("pages_failed is a LIST", isinstance(meta["pages_failed"], list))


# ---------------------------------------------------------------------------
# The engines — the checks CLAUDE.md §17 says to steal
# ---------------------------------------------------------------------------

def check_engines_import_their_driver_at_module_level():
    for module, driver in DRIVER_IMPORTS.items():
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver), driver in top,
              "top-level imports: %s" % sorted(top))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1. Every call from an engine (and the Scraper API client,
    diff_runs and page_flow itself) into a shared module is bound against the
    callee's real signature. A name that does not exist FAILS (§22). A name
    bound in the calling file shadows a same-named module."""
    import captcha_solver
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "captcha_solver": captcha_solver,
               "proxy_pool": proxy_pool}
    bound = 0
    for module in ENGINES + ("scraper_api_client", "diff_runs", "page_flow"):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        local_names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (targets[node.module], alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, owner, attr = node.func, None, None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in targets and func.value.id not in local_names):
                owner, attr = targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)" % (owner.__name__, attr,
                      module, node.lineno), False, "AttributeError on a live run")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                    isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*[None] * len(node.args), **{kw.arg: None for kw in node.keywords})
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr), False,
                      "%s; signature is %s" % (e, sig))
    check("the binding walk checked something (%d calls)" % bound, bound > 60,
          "only %d calls were bound — is the walk finding them?" % bound)


def _argparse_flags(module_name):
    tree = ast.parse(open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read())
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            parsers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


# The family's flag contract (CLAUDE.md §9, including the five it omitted
# for months), plus this repo's own query flags.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless", "--headful",
    "--fingerprint", "--fp-country", "--fp-tags", "--locale", "--mode",
}
BINANCE_FLAGS = {"--asset", "--fiat", "--side", "--pay-type", "--amount",
                 "--time-range", "--sort-by", "--order", "--hide-full"}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both
    ways. The exception list IS the documentation."""
    sets = {m: _argparse_flags(m) for m in ENGINES}
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | BINANCE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"}}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b), not only_a and not only_b,
              "only in %s: %s; only in %s: %s" % (a, sorted(only_a), b, sorted(only_b)))
    check("the documented difference still exists (closing it must be a decision)",
          "--chromium-path" in sets["puppeteer_scraper"])


def check_banned_and_removed_flags():
    """Scoped to the engines. `--country` is banned: it could disagree with
    the --url, and a P2P query's country is its --fiat."""
    for module in ENGINES:
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        for flag in ("--antidetect", "--country", "--country-code"):
            check("%s does not define %s" % (module, flag), '"%s"' % flag not in source)


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.
    Kept COARSE (pooled bindings) so it under-reports rather than invents."""
    import builtins
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved, repr(unresolved))


def check_no_statement_is_unreachable():
    """A statement after a return/raise/break/continue in the SAME block."""
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")


def _import_graph(entrypoint):
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, todo = set(), [entrypoint]
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def check_dockerfile_copies_everything_the_entrypoint_imports():
    dockerfile = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    missing = sorted(_import_graph("playwright_scraper") - copied)
    check("the Dockerfile COPYs every module playwright_scraper.py imports",
          not missing, "missing %s" % missing)
    check("the image does not carry the test suite", "smoke_test" not in copied)


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(read))
    check("the per-site variables carry the BINANCE_ prefix",
          {"BINANCE_CDP_ENDPOINT", "BINANCE_PROXY", "BINANCE_URL"} <= read)


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "BINANCE_CDP_ENDPOINT", "BINANCE_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in credentials:
                check("a copied .env.example leaves %s unset" % name, got is None, repr(got))
            else:
                check("...while %s stays a usable default" % name, got == raw.strip(), repr(got))
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read", env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # Inside the Docker image, which copies no .github at all. Triggered
        # by the WHOLE directory being absent, never by one file in it (§22).
        skip("ci_checks", "no .github directory (the image)")
        return
    check("the credential scan exists as a script", os.path.exists(script))
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml"),
                    encoding="utf-8").read()
    check("CI INVOKES the script rather than reimplementing it", "ci_checks.py" in workflow)
    result = subprocess.run([sys.executable, script, "--secret-check", "--sample-check"],
                            cwd=HERE, capture_output=True, text=True)
    check("the credential scan and sample check pass on this tree",
          result.returncode == 0, (result.stdout + result.stderr)[-600:])


def check_no_workflow_imports_the_code_inline():
    """The first push of this repo went red on an inline heredoc in
    tests.yml doing `from output_writer import Business`: the donor repo's
    row class, invisible to every local run because nothing local executes a
    workflow. A workflow calls ci_checks.py or the CLIs; it does not carry
    its own copy of a check that imports the code."""
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github directory (the image)")
        return
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(sorted(local)), re.M)
    for name in sorted(os.listdir(wf_dir)):
        hits = pattern.findall(open(os.path.join(wf_dir, name), encoding="utf-8").read())
        check("%s imports no local module inline" % name, not hits, repr(hits))


def check_the_hex_exemption_is_one_context_only():
    """SITE_PUBLIC_IDS forgives a 32-hex inside an announcement address and
    NOTHING else. Planted, not assumed."""
    if not os.path.isdir(os.path.join(HERE, ".github")):
        skip("ci_checks", "no .github directory (the image)")
        return
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks as C
    hexkey = "0123456789abcdef" * 2
    in_url = "https://www.binance.com/en/support/announcement/detail/" + hexkey
    check("inside the announcement address: forgiven",
          not C.HEX32.search(C._without_site_ids(in_url)))
    check("the same value elsewhere on ANOTHER line: still caught",
          bool(C.HEX32.search(C._without_site_ids('"key": "%s"' % hexkey))))
    check("inside some other binance.com path: still caught",
          bool(C.HEX32.search(C._without_site_ids("https://www.binance.com/en/x/" + hexkey))))


# Assembled from pieces, so this file can be scanned like every other rather
# than exempted (§22: the file most likely to acquire a stray phrase is the
# one a wholesale exemption never reads).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """§12, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in BANNED_WORDING:
                if phrase.lower() in text:
                    check("%s contains no banned phrase #%d" % (
                        os.path.relpath(path, HERE), BANNED_WORDING.index(phrase)), False)
    check("the banned-wording scan read the repo (%d files)" % scanned, scanned > 20)


def check_concurrency_with_the_browser_stubbed():
    """§10: a live run cannot always reach this machinery. Driven through the
    SHARED worker loop with the fetch replaced."""
    import page_flow
    import queue as queue_mod
    fetched, lock = [], threading.Lock()
    real = page_flow.fetch_one_page

    def fake(ops, args, pool, query, page_num, mask=None):
        with lock:
            fetched.append(page_num)
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [] if page_num >= 6 else [object()]
        o.state = "empty" if page_num >= 6 else "content"
        return o

    work = queue_mod.Queue()
    for n in range(2, 51):
        work.put(n)
    results, rlock, exhausted = [], threading.Lock(), threading.Event()
    args = types.SimpleNamespace(delay=0)
    page_flow.fetch_one_page = fake
    try:
        threads = [threading.Thread(target=page_flow.worker_loop,
                                    args=(_FakeOps({}), args, None, work, results,
                                          rlock, exhausted, "w%d" % i))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        page_flow.fetch_one_page = real
    check("every page fetched was fetched exactly once", len(fetched) == len(set(fetched)))
    check("dispatch STOPPED at the end of the listing", exhausted.is_set())
    check("...so 49 queued pages cost far fewer fetches", len(fetched) < 15,
          "fetched %d" % len(fetched))
    equal("attempted + unattempted covers the whole queue",
          len(set(fetched)) + work.qsize(), 49)


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    import page_flow

    def exploding(ops, args, pool, query, page_num, mask=None):
        if page_num == 3:
            raise RuntimeError("worker died")
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [object()]
        o.state = "content"
        return o

    class FakeOps(_FakeOps):
        def __init__(self, *a, **k):
            super().__init__({})

        def open(self):
            return self

    class FakePlaywright:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    real = (page_flow.fetch_one_page, engine._Ops, engine.sync_playwright)
    page_flow.fetch_one_page = exploding
    engine._Ops = FakeOps
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            types.SimpleNamespace(delay=0), None, None, list(range(2, 8)), 3)
    finally:
        page_flow.fetch_one_page, engine._Ops, engine.sync_playwright = real
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success", 3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    import page_flow
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    equal("three workers start on three different exits",
          len({page_flow.worker_pool(pool, i).current for i in range(3)}), 3)
    equal("a missing pool stays missing", page_flow.worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from fingerprint_client import playwright_context_kwargs
    import playwright.sync_api as pw_api
    sample = {"id": "x", "country": "US", "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US", "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown, repr(unknown))


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: wait_for_function evaluates a string, which a CSP without
    unsafe-eval kills. page.evaluate with a real function is fine."""
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called)


def check_fetch_js_is_one_request_in_three_dialects():
    """The one piece of JavaScript each engine spells its own way. It must
    make the same request, with the same timeout and the same cookie rule."""
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        js = re.search(r'FETCH_JS = """(.*?)"""', src, re.S)
        check("%s defines FETCH_JS" % module, js is not None)
        if not js:
            continue
        body = js.group(1)
        for needle in ('credentials: "include"', "AbortController",
                       'r.headers.get("x-amzn-waf-action")', '"content-type"'):
            check("%s's fetch() carries %s" % (module, needle), needle in body)


def check_credentials_never_reach_a_log():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module, "supersecret" not in masked, masked)
        check("%s keeps host and port" % module, "h1:9222" in masked and "h2:9222" in masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_aws_waf_solution_prefers_existing_token():
    """Measured 2026-09-24: `existing_token` set as the aws-waf-token cleared
    the CAPTCHA, a captcha_voucher set the same way did not. Driven through
    the real solve with requests stubbed — no network."""
    import captcha_solver as C
    ch = C.detect_aws_waf(fx("waf_captcha_chromium"), "https://www.binance.com/en/x")
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def make_post(solution):
        def post(url, json=None, timeout=None):
            sent.append(json)
            if "createTask" in url:
                return Resp({"errorId": 0, "taskId": 1})
            return Resp({"errorId": 0, "status": "ready", "solution": solution})
        return post

    real_post, real_sleep = C.requests.post, C.time.sleep
    C.time.sleep = lambda s: None
    try:
        C.requests.post = make_post({"captcha_voucher": "V", "existing_token": "E"})
        both = C._solve_with_2captcha_v2("k", ch)
        C.requests.post = make_post({"captcha_voucher": "V"})
        only_voucher = C._solve_with_2captcha_v2("k", ch)
        C.requests.post = make_post({"existing_token": "E", "bnc-uuid": "x"})
        only_existing = C._solve_with_2captcha_v2("k", ch)
    finally:
        C.requests.post, C.time.sleep = real_post, real_sleep
    equal("with both, existing_token is the cookie value", both, "E")
    equal("with only a voucher, the voucher", only_voucher, "V")
    equal("with existing_token and the site's cookies, existing_token", only_existing, "E")
    task = sent[0]["task"]
    equal("no proxy: the Proxyless task type", task["type"], "AmazonTaskProxyless")
    check("the task carries iv and context", task.get("iv") and task.get("context"))


def check_scraper_api_sends_waitfor_as_an_object_and_reads_http_code():
    """Measured 2026-09-23 against the live Scraper API: a JSON-encoded
    STRING waitFor is answered HTTP 422 and still billed; the target's status
    is `http_code`, while `status` is the API's own verdict."""
    try:
        import scraper_api_client as sac
    except ImportError as e:
        skip("scraper_api_client", str(e))
        return
    sent = {}

    class Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def post(url, **kw):
        sent.update(kw.get("json") or {})
        return Resp()

    args = types.SimpleNamespace(url="https://www.binance.com/bapi/x", key="k" * 8,
                                 timeout=60, cdp_url=None, wait_text="000000",
                                 wait_element=None, wait_state=None)
    real = sac.requests.post
    sac.requests.post = post
    try:
        _html, status = sac.fetch_html(args)
    finally:
        sac.requests.post = real
    equal("--wait-text sends waitFor as an OBJECT", sent.get("waitFor"), {"text": "000000"})
    equal("the target status handed onward is http_code", status, 403)
    equal("the Scraper API response's JSON is unwrapped from a viewer <pre>",
          sac.json_text('<html><body><pre>{"code":"000000"}</pre></body></html>'),
          '{"code":"000000"}')


def check_x_debug_header_is_redacted():
    try:
        import scraper_api_client as sac
    except ImportError:
        return
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    check("x-debug: the credential and the key are gone", pw not in out and key not in out)
    check("x-debug: the cost, host and status survive",
          "cost=0.00145" in out and "cb.2captcha.com:9222" in out and "status=200" in out)
    check("x-debug: the log line calls the redactor",
          'logger.info("x-debug: %s", _redact_debug_header(debug))' in inspect.getsource(sac))


def check_captcha_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    solver = open(os.path.join(HERE, "captcha_solver.py"), encoding="utf-8").read()
    low = readme.lower()
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in low)
    check("the solver builds AmazonTask, which the README credits",
          "AmazonTask" in solver and "amazontask" in low)


def check_readme_numbers_are_not_stale():
    """§17's check #4: a column count claimed in the README is a class's."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import ROW_CLASS_BY_MODE
    sizes = {len(fields(c)) for c in ROW_CLASS_BY_MODE.values()}
    for number in re.findall(r"(\d+)\s+columns", readme):
        check("the README's '%s columns' is a row class's size" % number,
              int(number) in sizes, "sizes are %s" % sorted(sizes))
    import product_parser as P
    for claim, value in (("20 adverts", P.P2P_ROWS), ("30 portfolios", P.COPY_ROWS),
                         ("50 announcements", P.ANN_ROWS)):
        n = int(claim.split()[0])
        if claim in readme:
            equal("the README's %r matches the code" % claim, value, n)


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines() if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    changed = sorted(set(_tree_state()) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree", not changed, repr(changed))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE, _TREE_BEFORE
    parser = argparse.ArgumentParser(description="binance-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = parser.parse_args().verbose
    _TREE_BEFORE = _tree_state()
    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()
    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
