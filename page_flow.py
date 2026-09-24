"""
page_flow.py
------------
The retry / solve / blocked decision, as DATA rather than as three copies of
an if-chain (CLAUDE.md §1), and the one fetch loop all three engines share.

makemytrip.com answers one of this repo's requests in seven ways, and they
want five different responses:

    the listing API's JSON with properties in it     -> parse
    the same with none, or "No Hotels Found"         -> parse, it is an answer
    an error envelope with any other code            -> stop: the PARAMETERS
                                                        were refused, and a
                                                        retry sends them again
    Akamai's "Access Denied" (HTTP 403)              -> rotate
    the connection reset / HTTP/2 or TLS error       -> rotate: Akamai drops
                                                        the address rather
                                                        than answering it
    429                                              -> wait, same exit
    anything else                                    -> retry

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine spells its fetch() in its own driver's dialect.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (MAX_PAGES, ORIGIN_URL, Cursor, Query, Room,
                            api_error, currency_of, default_dates,
                            detect_page_state, is_location_code, listing_ended,
                            listing_url, location_of, next_cursor,
                            only_substitutes, parse_date, parse_page,
                            pick_suggestion, query_from_url, request_for,
                            section_names, suggest_request)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# How long one fetch() may take before the engine gives up on it. The
# largest response measured was 374 KB (30 Dubai properties), which arrived
# in about a second. The bound exists because a browser fetch() has no
# timeout of its own, and CLAUDE.md §8 requires every remote call to have one.
FETCH_TIMEOUT_MS = 30_000

# How long to wait at the SAME exit after a 429 before trying again. Not
# observed on this site; the family's answer to a throttle is to slow down
# rather than to rotate (§24).
THROTTLE_WAIT_S = 10.0
THROTTLE_RETRIES = 2

# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url, waf_action)`. A sibling repo shipped
    `classify(html, url=...)` in two of three engines against a callee that
    took `status` second, and both crashed on their first fetch (§17).
    `smoke_test.py` binds every engine's call against this signature for
    that reason.
    """
    return detect_page_state(html or "", status, url, waf_action)


STATE_POLICY = {
    "content":    {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A city with nothing to offer for these dates, or a code the site does
    # not know ("No Hotels Found", code 400814). The site served exactly what
    # was asked for, so this is EXIT_NO_PRODUCTS rather than EXIT_BLOCKED.
    "empty":      {"retry": False, "solve": False, "blocked": False, "parse": True},
    # The endpoint refused the PARAMETERS (code 400108 "field name is not
    # supported", for one). The same request sent again gets the same
    # answer, so nothing retries and nothing counts as blocked.
    "rejected":   {"retry": False, "solve": False, "blocked": False, "parse": False},
    # A captcha on the landing. NOT OBSERVED on this site (product_parser's
    # docstring); here so a widget that ever appears is solved rather than
    # reported as a timeout.
    "challenge":  {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
    # Rate limited: wait at the same exit (THROTTLE_*). NOT counted as
    # blocked, since calling a throttle a block reports exit 3 for a page
    # that was about to come back (§24). Not observed on this site.
    "throttled":  {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # Akamai's "Access Denied", or HTTP 403.
    "blocked":    {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Akamai dropping the connection instead of answering: an HTTP/2
    # protocol error, a reset, a TLS failure. From a datacentre address this
    # is EVERY answer (Chromium, headless and headful, 2026-09-24); through
    # the Scraping Browser it was 1 of 6 fresh connections, and a reconnect
    # was served from a different Indian exit. So it is a refusal of the
    # ADDRESS, and the response is a different one, not a wait.
    "reset":      {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Not JSON and not an interstitial. Worth one more try.
    "unknown":    {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the loop,
# so setting it False really does stop the retry loop (§17).
RETRY_ON_BLOCKED = True

# How many times to re-fetch a refused page when there is no proxy pool to
# rotate into. Two, not the family's usual one: over the Scraping Browser a
# refused connection is followed by a fresh one, and the service picked a
# different exit for it (measured: 183.87.x refused, 223.178.x served,
# 2026-09-24). From a local browser with no pool the answer does not change,
# and two more refusals cost about thirty seconds.
BLOCK_RETRIES_WITHOUT_POOL = 2

# At most one solve per page. A challenge that survives a solved token is not
# a challenge this run can pass, and a second solve is a second charge for
# the same answer.
SOLVES_PER_PAGE = 1

# Chromium's names for a connection Akamai dropped rather than answered.
# Distinct from a timeout (retry at the same exit) and from a proxy failure
# (the proxy itself is broken): here the proxy, or the direct route, worked
# and the SITE hung up.
RESET_MARKERS = (
    "ERR_HTTP2_PROTOCOL_ERROR",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_EMPTY_RESPONSE",
)


def is_reset(error_text: Optional[str]) -> bool:
    return any(m in (error_text or "") for m in RESET_MARKERS)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def concurrency_refusal(concurrency: int) -> Optional[str]:
    """Why --concurrency above 1 is refused, or None when it is 1.

    Refused rather than quietly clamped (§18): page N's request carries a
    cursor that only page N-1's response names, so there is nothing a
    second worker could fetch.
    """
    if concurrency <= 1:
        return None
    return ("--concurrency %d is refused: the listing is paged by a cursor "
            "that each response hands to the next request, so page N cannot "
            "be fetched before page N-1. For volume, run several cities "
            "in parallel, one process each." % concurrency)


# ---------------------------------------------------------------------------
# Refusals: how they are named and what the reader is told
# ---------------------------------------------------------------------------

def refusal_name(state: str) -> str:
    """The name a refusal is reported by, in logs and in `stop_reason`."""
    return {"blocked": "akamai", "reset": "akamai-reset",
            "challenge": "captcha"}.get(state, state)


def refusal_advice(state: str) -> str:
    """One sentence on what changes the answer, per refusal. Kept here so
    the three engines cannot give three different pieces of advice."""
    if state in ("blocked", "reset"):
        return ("Akamai refuses this ADDRESS: from a datacentre every client "
                "was refused, real Chromium included, and nothing on the page "
                "can be solved. What was measured to work is the Scraping "
                "Browser with an Indian exit: --cdp-endpoint with "
                "`country-in` in the login (MAKEMYTRIP_CDP_ENDPOINT in .env).")
    return ("A captcha stood in front of the landing. Set TWOCAPTCHA_KEY so it "
            "can be solved, or use --cdp-endpoint, whose browser can clear it "
            "itself.")


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "rejected", None):
        return "api_rejected"
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The query flags, by the argparse dest they land in. A flag left at None was
# not typed, which is how build_query tells a user's value from a default.
QUERY_FLAGS = (("city", "--city"), ("checkin", "--checkin"),
               ("checkout", "--checkout"), ("adults", "--adults"),
               ("rooms", "--rooms"), ("child_age", "--child-age"))


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url or from the flags, validated.

    --url and the query flags are two ways to say the same thing. A flag the
    user typed that disagrees with the URL would silently scrape something
    neither of them named, so the combination is refused rather than merged
    (the family's --country rule, §10). `error` is argparse's `p.error`, so
    a refusal is exit 2 with the usage line, as in every engine. --sort is
    not a query flag in that sense: the site's listing address carries no
    ordering, so it applies to either.
    """
    refusal = concurrency_refusal(getattr(args, "concurrency", 1) or 1)
    if refusal:
        error(refusal)
    if args.url:
        query, why = query_from_url(args.url)
        if query is None:
            error(why)
        typed = [flag for dest, flag in QUERY_FLAGS
                 if getattr(args, dest, None) is not None]
        if typed:
            error("--url already carries the query; %s would have to agree "
                  "with it and nothing checks that they do. Pass a URL or "
                  "the flags, not both." % ", ".join(typed))
    else:
        checkin = checkout = None
        if args.checkin is not None:
            checkin = parse_date(args.checkin)
            if checkin is None:
                error("--checkin %r is not a date (YYYY-MM-DD)" % args.checkin)
        if args.checkout is not None:
            checkout = parse_date(args.checkout)
            if checkout is None:
                error("--checkout %r is not a date (YYYY-MM-DD)" % args.checkout)
        if checkin is None and checkout is None:
            checkin, checkout = default_dates()
            log.info("No dates given: searching one night from %s, 30 days "
                     "ahead. Prices are per night and depend on the dates.",
                     checkin.isoformat())
        elif checkout is None and checkin is not None:
            checkout = checkin.fromordinal(checkin.toordinal() + 1)
        ages = tuple(args.child_age or ())
        adults = args.adults if args.adults is not None else 2
        n_rooms = args.rooms if args.rooms is not None else 1
        # Guests go into the first room and the rest get one adult each,
        # which is how the site's own form fills extra rooms.
        rooms = tuple([Room(adults=adults, child_ages=ages)]
                      + [Room(adults=1) for _ in range(max(0, n_rooms - 1))])
        city = (args.city or "").strip()
        code = city.upper() if is_location_code(city.upper()) else None
        query = Query(city=city or None, city_code=code,
                      country_code="IN" if code else None,
                      checkin=checkin, checkout=checkout, rooms=rooms)
    query.sort = args.sort or query.sort
    why = query.validate()
    if why:
        error(why)
    if args.pages < 1:
        error("--pages must be at least 1")
    if args.pages > MAX_PAGES:
        error("--pages is capped at %d" % MAX_PAGES)
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it (diff_runs.py compares these)."""
    return {"city_code": query.city_code, "country_code": query.country_code,
            "checkin": query.checkin.isoformat() if query.checkin else None,
            "checkout": query.checkout.isoformat() if query.checkout else None,
            "rooms": [{"adults": r.adults, "child_ages": list(r.child_ages)}
                      for r in query.rooms],
            "sort": query.sort}


def finish(args, query: Query, outcomes: List, stop_reason: str,
           blocked: bool) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6).
    """
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key="sku")
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s).", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    substitutes = sum(1 for r in rows if r.section and r.section != "RECOMMENDED_HOTELS")
    if substitutes:
        log.warning("%d of %d row(s) are the site's SUBSTITUTES (section != "
                    "RECOMMENDED_HOTELS): properties outside %s that it listed "
                    "once it had nothing more there. Filter on `section` if you "
                    "want the city only.", substitutes, len(rows),
                    query.city_name or query.city_code)
    first = next((o for o in outcomes if o.page_num == 1), None)
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=args.pages, pages_completed=len(ok_pages),
        pages_failed=failed_pages, mode=query.mode, source=SOURCE_DEFAULT,
        start_url=args.url or listing_url(query),
        final_url=listing_url(query),
        extra={"query": query_summary(query),
               "city_name": query.city_name,
               "end_of_listing": stop_reason == "end_of_listing",
               "substitute_rows": substitutes,
               "currency": getattr(first, "currency", None)})


# ---------------------------------------------------------------------------
# The fetch loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: landing on the
# origin, paying for a captcha if one ever appears, retrying a transport
# failure, waiting out a throttle, re-connecting on a refusal, and parsing
# what came back. The three engines differ only in HOW they ask their
# driver, so each passes in an object with these operations and no
# JavaScript crosses this boundary (§1):
#
#     ops.goto(url)        -> (status, waf_header). Raises TransportError.
#     ops.document_text()  -> the landing document's markup
#     ops.wait_ms(ms)
#     ops.solve_captcha()  -> True if a captcha was solved and the page reloaded
#     ops.fetch(req)       -> (status, text, waf_header, error_or_None)
#     ops.relaunch()       -> a fresh browser (on the pool's current exit), or
#                             a fresh connection to the remote one
#     ops.landed           -> bool attribute, owned by the loop
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""


class TransportError(Exception):
    """A navigation that did not complete: a timeout, a reset, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on the order things finished in (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    rejected: Optional[str] = None
    # Where the next page starts, and whether the site said nothing does.
    cursor: Optional[Cursor] = None
    ended: bool = False
    currency: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (not self.load_failed and self.blocked_by is None
                and self.rejected is None)


# The columns the site filled on EVERY record of every capture (114
# properties across Goa, Mumbai and Dubai, 2026-09-24). Below this share,
# the payload shape has moved rather than the data being unusual.
# Deliberately NOT here: `rating` (null on 23 of 114, which had no ratings),
# `star_rating` (null on unstarred homestays and hostels) and
# `original_price` (null where there is no discount).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = {
    "hotels": ("sku", "title", "price", "price_with_tax", "currency",
               "property_type", "latitude", "url"),
}


def land(ops, args) -> Tuple[str, Optional[str]]:
    """Put the page on a www.makemytrip.com document that fetch() can use.

    Returns (state, error): state is a policy state for the landing, and
    error is a transport failure's text or None. A reset connection is a
    STATE ("reset"), not an error, because it is Akamai's answer rather than
    a fault on the way to it.
    """
    try:
        status, waf = ops.goto(ORIGIN_URL)
    except TransportError as e:
        if is_reset(str(e)):
            ops.landed = False
            return "reset", None
        return "load_failed", str(e)
    state = classify(ops.document_text(), status, ORIGIN_URL, waf)
    if state == "challenge" and should_solve(state):
        for _ in range(SOLVES_PER_PAGE):
            if not ops.solve_captcha():
                break
            state = classify(ops.document_text(), None, ORIGIN_URL, None)
            if state != "challenge":
                break
    ops.landed = state == "content"
    return state, None


def _core_field_warnings(rows: List, mode: str, page_num: int) -> None:
    for name in CORE_FIELDS.get(mode, ()):
        if not rows:
            return
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every record of every capture "
                        "had one, so the payload shape has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)


def _dump(args, page_num: int, text: str) -> None:
    """Write the exact response the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the response the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   cursor: Optional[Cursor] = None,
                   mask: Callable[[str], str] = lambda s: s) -> PageOutcome:
    """Fetch and parse one page. Retries, re-connections and dumps live here.

    Never raises for an EXPECTED failure. A timeout, a refusal and a dead
    exit are all recorded on the outcome.
    """
    req = request_for(query, page_num, cursor)
    outcome = PageOutcome(page_num=page_num, url=req.label)

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool else BLOCK_RETRIES_WITHOUT_POOL)
    throttles = 0
    state, text, last_error, exit_failed = "unknown", "", None, None

    for block_attempt in range(block_retries + 1):
        log.info("Fetching page %d/%d: %s", page_num, args.pages, req.label)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            text, last_error = "", None
            if not ops.landed:
                state, last_error = land(ops, args)
                if last_error:
                    exit_failed = ops.proxy_failure(last_error) or None
                    if exit_failed:
                        break  # a different exit is the only thing that helps
                elif not ops.landed:
                    text = "" if state == "reset" else ops.document_text()
            if ops.landed:
                status, text, waf, last_error = ops.fetch(req)
                if last_error and is_reset(last_error):
                    state, last_error = "reset", None
                else:
                    state = ("load_failed" if last_error
                             else classify(text, status, req.url, waf))
                if counts_as_blocked(state):
                    # Refused after the landing: the next attempt lands again.
                    ops.landed = False
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on page %d (HTTP 429) — waiting "
                            "%.0fs at the same exit (%d/%d).", page_num, pause,
                            throttles, THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if state in ("load_failed", "unknown") and attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("Page %d came back %s (attempt %d/%d)%s — retrying "
                            "in %.1fs.", page_num, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            if has_pool:
                log.warning("Page %d refused (%s) at %s — rotating to another "
                            "exit (%d/%d).", page_num, refusal_name(state),
                            mask(pool.current), block_attempt + 1, block_retries)
                pool.advance(f"refused: {refusal_name(state)}")
            else:
                log.warning("Page %d refused (%s) — trying again from a fresh "
                            "browser (%d/%d).", page_num, refusal_name(state),
                            block_attempt + 1, block_retries)
            ops.relaunch()
            continue
        break

    outcome.state = state
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on page %d: %s", page_num,
                  mask(last_error or "the request never completed"))
        return outcome
    if state == "rejected":
        outcome.rejected = api_error(text) or "an error envelope with no code"
        log.error("The endpoint refused this request (%s). That is a statement "
                  "about the PARAMETERS, and the same request sent again gets "
                  "the same answer, so it is not retried. If the query looks "
                  "right, the site's API has changed — open an issue with "
                  "--dump-html.", outcome.rejected)
        _dump(args, page_num, text)
        return outcome
    if counts_as_blocked(state):
        outcome.blocked_by = refusal_name(state)
        debug = _save_debug(args, page_num, text)
        log.error("Blocked by %s on page %d — saved to %s. This is exit 3, "
                  "distinct from an empty listing (exit 4). %s",
                  outcome.blocked_by, page_num, debug, refusal_advice(state))
        return outcome
    if not should_parse(state):
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as the listing's JSON (%s) — saved "
                  "to %s. %s", page_num, state, debug,
                  "Raise --delay." if state == "throttled" else "")
        return outcome

    _dump(args, page_num, text)
    if state == "empty" and api_error(text):
        log.info("The site answered page %d with %s.", page_num, api_error(text))
    rows = parse_page(text, query, page_num)
    outcome.products = rows
    outcome.cursor = next_cursor(text, cursor)
    outcome.ended = listing_ended(text) or (bool(rows) and outcome.cursor is None)
    outcome.currency = currency_of(text)
    log.info("Parsed %d row(s) from page %d (%s).", len(rows), page_num,
             ", ".join(section_names(text)) or "no sections")
    if page_num == 1:
        _check_location(text, query)
    if rows and only_substitutes(text):
        log.warning("Page %d holds only the site's substitutes (%s), not "
                    "properties in %s.", page_num, ", ".join(section_names(text)),
                    query.city_name or query.city_code)
    _core_field_warnings(rows, query.mode, page_num)
    return outcome


def _check_location(text: str, query: Query) -> None:
    """Warn when the response is about somewhere other than what was asked.

    A code the site half-knows is answered with a different town: CTLEH
    came back as "Lehra" with every property in Patran (2026-09-24).
    """
    loc = location_of(text)
    if loc["code"] and query.city_code and loc["code"] != query.city_code:
        log.warning("Asked for %s; the site answered about %s (%s).",
                    query.city_code, loc["code"], loc["name"])
    elif loc["name"]:
        log.info("The site reports this listing as %s (%s, %s).", loc["name"],
                 loc["code"], loc["country"])
    if loc["country"] and query.country_code and loc["country"] != query.country_code:
        log.warning("The site places %s in country %s, not %s — pass --city by "
                    "NAME so the site's own lookup supplies both.",
                    loc["code"], loc["country"], query.country_code)


def resolve_city(ops, args, query: Query) -> Tuple[Optional[str], Optional[str]]:
    """Fill in city_code/country_code from a city NAME, via the site's own
    autosuggest. Returns (refusal, blocked_state): a reason the run is
    refused (exit 2), or the refusal STATE that stopped the lookup, which
    the run reports as blocked like any page (exit 3).

    Asked before page 1 because the listing endpoint answers an unknown or
    half-known code with plausible data about somewhere else (see
    product_parser's docstring), and the search box's own lookup is what a
    person typing "Goa" gets.
    """
    if query.city_code:
        return None, None
    last_block = None
    for _ in range(max(1, args.retries) + BLOCK_RETRIES_WITHOUT_POOL):
        if not ops.landed:
            state, err = land(ops, args)
            if not ops.landed:
                if counts_as_blocked(state):
                    last_block = state
                    ops.relaunch()
                continue
        status, text, _waf, err = ops.fetch(suggest_request(query.city))
        if err:
            if is_reset(err):
                ops.landed = False
                ops.relaunch()
            continue
        if status == 403:
            last_block = "blocked"
            ops.landed = False
            ops.relaunch()
            continue
        picked, why = pick_suggestion(text, query.city)
        if picked is None:
            return why, None
        query.city_code, query.country_code = picked["code"], picked["country"]
        query.city_name = picked["name"]
        log.info("%r -> %s (%s), as the site's own search box resolves it.",
                 query.city, picked["code"], picked["display"])
        return None, None
    if last_block:
        return None, last_block
    return ("Could not reach the site's autosuggest to look up %r. Pass the "
            "city's code instead (CTGOI for Goa), or a listing --url."
            % query.city), None


def run_pages(open_ops, close_ops, args, pool, query: Query,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, and `close_ops(ops)`
    tears it down. The pages are fetched one after another, each with the
    cursor the previous one returned.
    """
    outcomes: List[PageOutcome] = []
    blocked, stop_reason = False, "completed"
    ops = open_ops()
    try:
        refusal, refused_state = resolve_city(ops, args, query)
        if refusal:
            log.error("%s", refusal)
            return 2
        if refused_state:
            # The lookup never got an answer because the site refused the
            # address: that is this run's page 1, blocked, with the advice.
            outcomes.append(PageOutcome(page_num=1, url="autosuggest",
                                        state=refused_state,
                                        blocked_by=refusal_name(refused_state)))
            log.error("Blocked by %s before the city could be looked up. %s",
                      refusal_name(refused_state), refusal_advice(refused_state))
            return finish(args, query, outcomes,
                          "blocked_%s" % refusal_name(refused_state), True)
        cursor: Optional[Cursor] = None
        for page_num in range(1, args.pages + 1):
            if page_num > 1:
                ops.wait_ms(int(args.delay * 1000))
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    ops.relaunch()
            outcome = fetch_one_page(ops, args, pool, query, page_num, cursor, mask)
            outcomes.append(outcome)
            if not outcome.ok:
                stop_reason = stop_reason_for(outcome)
                blocked = outcome.blocked_by is not None
                break
            if outcome.ended or not outcome.products:
                log.info("The site says the listing ends at page %d.", page_num)
                stop_reason = "end_of_listing"
                break
            cursor = outcome.cursor
    finally:
        if ops is not None:
            close_ops(ops)
    return finish(args, query, outcomes, stop_reason, blocked)


def cdp_connect_hint(error_text: str) -> str:
    """What a failed --cdp-endpoint connection means, from its status.

    Two answers that want opposite fixes. Measured 2026-09-24 against four
    Scraping Browser endpoints left in sibling repos' .env files: all four
    answered 401 Unauthorized, because a profile's credentials last about a
    day. The message used to explain a 500 (a pid another run still holds)
    whatever the status was, which sent the reader to wait for a run that
    did not exist.
    """
    if "401" in (error_text or ""):
        return ("HTTP 401: the endpoint's credentials were refused. A Scraping "
                "Browser profile's credentials last about a day, so an "
                "endpoint copied from an older .env has usually expired. "
                "Get a fresh one from your 2Captcha dashboard.")
    return ("A Scraping Browser profile allows ONE live connection at a time, "
            "so an HTTP 500 here usually means another run still holds this "
            "`pid`. Wait for it to finish, or use a different pid.")


# Connecting to a Scraping Browser profile right after the previous run let
# go of it answers HTTP 500 `profile_locked`: the service releases a profile
# 1.6-1.9 s after a clean disconnect (measured 3 of 3, 2026-09-24). Two
# back-to-back runs therefore failed with exit 5 in the first live matrix
# through --cdp-endpoint. Three attempts 3 s apart ride that out, and a
# profile genuinely held by another run still fails, after ~9 s, with the
# pid explanation.
CDP_CONNECT_ATTEMPTS = 3
CDP_LOCKED_WAIT_S = 3.0
# pyppeteer does not surface the 500 at all: its connect() waits on a future
# the rejected handshake never resolves, so only a timeout ends it. A
# successful connect measured 0.8-0.95 s, so 10 s is an order of magnitude
# of headroom and a third of the 30 s it used to wait per attempt.
CDP_CONNECT_TIMEOUT_S = 10


def cdp_should_retry(error_text: str) -> bool:
    """Whether a failed --cdp-endpoint connection is worth another attempt:
    a locked profile (500) or a connect that never answered. A 401 is not:
    expired credentials stay expired."""
    text = error_text or ""
    if "401" in text:
        return False
    return ("profile_locked" in text or " 500" in text or "HTTP 500" in text
            or "did not return within" in text)
