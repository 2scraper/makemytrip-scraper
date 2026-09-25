#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for makemytrip-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and checks that engine imports.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They are real
responses captured 2026-09-24, trimmed and scrubbed by `make_fixtures.py`,
which proves each one parses (or classifies) identically to its untrimmed
original. Not verbatim: the capturing session's visitor, device and session
ids and each response's `correlationKey` are placeholders, and the keys the
parser never reads were dropped (see make_fixtures.py).
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
import types
from dataclasses import asdict, fields
from datetime import date, timedelta

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
    """A fixture as the text the site returns."""
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


def _q(code="CTGOI", country="IN", **kw):
    from product_parser import Query
    return Query(city=code, city_code=code, country_code=country,
                 checkin=date(2026, 10, 15), checkout=date(2026, 10, 16), **kw)


def _rows(name, **kw):
    import product_parser as P
    return P.parse_page(fx(name), _q(**kw), 1)


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    expected = {"search_goa_p1", "search_goa_p2", "search_goa_price_asc",
                "search_dubai", "search_lehra_nearby", "search_no_expdata",
                "search_no_hotels", "search_bad_sort", "suggest_goa",
                "suggest_dubai", "suggest_taj_mahal", "suggest_nothing",
                "akamai_denied_www", "akamai_denied_api", "akamai_decoy",
                "chrome_error_page", "landing_served", "ssr_listing_goa",
                "cdp_extension_injection"}
    missing = expected - set(FIXTURES)
    check("every fixture the suite uses is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 50000, "%d bytes" % len(blob))
    import make_fixtures
    check("no id of the capturing session survived the scrub",
          not any(sid in blob for sid in make_fixtures.SESSION_IDS))
    check("...and the placeholders are what replaced them",
          "fixture-correlation-key" in blob)


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage (§10)
# ---------------------------------------------------------------------------

def check_goa_listing_parses_to_the_captured_values():
    rows = _rows("search_goa_p1")
    equal("four properties on the trimmed Goa page", len(rows), 4)
    r = rows[0]
    equal("sku is the site's hotel id", r.sku, "202410211734063978")
    equal("title", r.title, "Ginger Goa, Candolim")
    equal("price: per night, before taxes, after the discount",
          (r.price, r.price_with_tax, r.price_with_fees), (3884.0, 4514.0, 4514.0))
    equal("original_price is the struck-through figure", r.original_price, 4499.0)
    equal("discount_pct is COMPUTED from the two", r.discount_pct, 13.7)
    equal("currency is the response's own", r.currency, "INR")
    equal("stars, rating and its counts",
          (r.star_rating, r.rating, r.rating_count, r.review_count),
          (4, 4.2, 3941, 1962))
    equal("whose reviews", r.review_source, "MMT")
    equal("the section is the city's own", r.section, "RECOMMENDED_HOTELS")
    equal("locality, city and the site's country code",
          (r.locality, r.city_code, r.country_code), ("Candolim", "CTGOI", "IN"))
    check("the url is the property's own detail page for this stay",
          r.url.startswith("https://www.makemytrip.com/hotels/hotel-details?hotelId=202410211734063978"))
    check("the image is absolute (the site writes it protocol-relative)",
          r.image.startswith("https://"))
    equal("price basis as the site states it", r.price_basis, "Per Night")
    equal("the query rides on the row",
          (r.check_in, r.check_out, r.adults, r.rooms, r.sort),
          ("2026-10-15", "2026-10-16", 2, 1, "popular"))
    no_disc = rows[1]
    equal("no struck-through price: original and discount are None, not 0",
          (no_disc.original_price, no_disc.discount_pct), (None, None))
    hostel = rows[2]
    equal("a hostel with starRating 0 has NO stars, not nought stars",
          (hostel.property_type, hostel.star_rating), ("Hostel", None))


def check_unrated_properties_carry_no_zero_rating():
    """§21: zero is not a rating. Taken from a real record with its rating
    counts zeroed, which is how the site writes an unrated property."""
    import product_parser as P
    payload = copy.deepcopy(FIXTURES["search_goa_p1"])
    rec = payload["response"]["personalizedSections"][0]["hotels"][0]
    rec["reviewSummary"].update(cumulativeRating=0.0, totalRatingCount=0,
                                totalReviewCount=0)
    r = P.parse_page(json.dumps(payload), _q(), 1)[0]
    equal("rating, counts, text and source go null TOGETHER",
          (r.rating, r.rating_count, r.review_count, r.rating_text, r.review_source),
          (None, None, None, None, None))
    rows = [row for name in ("search_goa_p1", "search_goa_p2", "search_dubai",
                             "search_lehra_nearby", "search_goa_price_asc")
            for row in _rows(name, code="CTDUB" if name == "search_dubai" else "CTGOI")]
    check("no row anywhere carries a 0 rating or 0 stars",
          not any(row.rating == 0 or row.star_rating == 0 for row in rows))
    check("no row has an original price at or below its price (§4's canary)",
          not any(row.original_price is not None and row.original_price <= row.price
                  for row in rows))


def check_dubai_prices_carry_the_fees_paid_at_the_property():
    rows = _rows("search_dubai", code="CTDUB", country="UNI")
    r = rows[0]
    equal("Dubai is priced in the site's REGION currency", r.currency, "INR")
    equal("with taxes, then with the city tax paid at the property",
          (r.price, r.price_with_tax, r.price_with_fees), (4444.0, 5444.0, 5705.0))
    check("every Dubai row: fees are on top of the taxes",
          all(x.price_with_fees > x.price_with_tax for x in rows))
    equal("the site's country code for the UAE is UNI, not ISO", r.country_code, "UNI")
    equal("review sources differ per property",
          sorted({x.review_source for x in rows}), ["MMT_BKG", "MMT_EXP"])


def check_a_request_without_expdata_is_answered_with_no_prices():
    """Measured: without the front end's `expData`, every property comes back
    with no priceDetail. The fixture is that answer; the request must carry
    the string."""
    import product_parser as P
    rows = _rows("search_no_expdata")
    check("the captured answer has properties (not vacuous)", len(rows) == 2)
    check("...and not one price among them",
          all(r.price is None and r.price_with_tax is None for r in rows))
    body = P.request_for(_q(), 1).body
    equal("the request carries expData verbatim", body.get("expData"), P.EXP_DATA)
    check("...the captured string, not an empty one", "PDO:PN" in P.EXP_DATA)


def check_the_cursor_threads_from_one_page_to_the_next():
    import product_parser as P
    cur = P.next_cursor(fx("search_goa_p1"))
    resp = FIXTURES["search_goa_p1"]["response"]
    equal("the cursor is the server's own last id and window",
          (cur.last_hotel_id, cur.window), (resp["lastHotelId"], resp["lastFetchedWindowInfo"]))
    equal("...and counts what has been shown", cur.shown, 4)
    later = P.next_cursor(fx("search_goa_p2"), cur)
    equal("the count accumulates across pages", later.shown, 7)
    body = P.request_for(_q(), 2, cur).body["searchCriteria"]
    equal("page 2's request sends the cursor back",
          (body["lastHotelId"], body["lastFetchedWindowInfo"], body["totalHotelsShown"]),
          (resp["lastHotelId"], resp["lastFetchedWindowInfo"], 4))
    first = P.request_for(_q(), 1).body["searchCriteria"]
    equal("page 1 starts with an empty cursor",
          (first["lastHotelId"], first["lastFetchedWindowInfo"], first["totalHotelsShown"]),
          ("", "", 0))
    equal("the page size is the measured one", first["limit"], P.ROWS_PER_PAGE)
    equal("...30", P.ROWS_PER_PAGE, 30)


def check_a_half_known_code_is_answered_with_substitutes():
    """CTLEH came back as 'Lehra' with every property somewhere else."""
    import product_parser as P
    rows = _rows("search_lehra_nearby", code="CTLEH")
    check("the substitute page has rows (not vacuous)", len(rows) == 3)
    check("every row says it is a substitute",
          all(r.section == "NEARBY_HOTELS" for r in rows))
    check("...from other towns", {r.city_code for r in rows} <= {"CTPATR", "CTTOH"})
    check("only_substitutes sees it", P.only_substitutes(fx("search_lehra_nearby")))
    check("...and not on a real city page", not P.only_substitutes(fx("search_goa_p1")))
    check("the site says the listing ended", P.listing_ended(fx("search_lehra_nearby")))
    check("...and not on Goa's first page", not P.listing_ended(fx("search_goa_p1")))
    equal("the response names the place it thinks it is about",
          P.location_of(fx("search_lehra_nearby"))["name"], "Lehra")


def check_position_counts_emitted_rows():
    import product_parser as P
    payload = copy.deepcopy(FIXTURES["search_goa_p1"])
    hotels = payload["response"]["personalizedSections"][0]["hotels"]
    hotels.insert(1, {"name": "no id"})       # a record the parser drops
    rows = P.parse_page(json.dumps(payload), _q(), 1)
    equal("a dropped record does not shift later positions (§24)",
          [r.position for r in rows], [1, 2, 3, 4])


def check_the_server_rendered_listing():
    import product_parser as P
    listing = P.initial_state_listing(fx("ssr_listing_goa"))
    check("the page's own state is found", listing is not None)
    rows = P.parse_page(listing, _q(), 1)
    equal("its properties parse in the API's shape", len(rows), 3)
    equal("the first is the same property the API ranks first",
          rows[0].sku, "202410211734063978")
    equal("its currency is the page's own `searchHotelsCurrency`",
          {r.currency for r in rows}, {"INR"})
    check("a page with a sponsored property says so (not vacuous)",
          any(r.sponsored for r in rows))
    equal("a page without the state gives None", P.initial_state_listing(fx("landing_served")), None)


# ---------------------------------------------------------------------------
# Queries, URLs and the API's plausible answers to mistakes
# ---------------------------------------------------------------------------

def check_url_shapes():
    import product_parser as P
    q, why = P.query_from_url(
        "https://www.makemytrip.com/hotels/hotel-listing/?checkin=10152026&city=CTGOI"
        "&checkout=10162026&roomStayQualifier=2e0e&locusId=CTGOI&country=IN&locusType=city")
    check("the site's own listing address is read", q is not None, why or "")
    equal("...city, country, dates and guests",
          (q.city_code, q.country_code, q.checkin, q.checkout, q.adults),
          ("CTGOI", "IN", date(2026, 10, 15), date(2026, 10, 16), 2))
    q2, _ = P.query_from_url(
        "https://www.makemytrip.com/hotels/hotel-listing/?checkin=10152026&checkout=10172026"
        "&city=CTDUB&locusId=CTDUB&locusType=city&country=UNI&roomStayQualifier=2e2e5e8e1e0e")
    equal("two rooms, two children with their ages",
          [(r.adults, r.child_ages) for r in q2.rooms], [(2, (5, 8)), (1, ())])
    equal("the qualifier round-trips", P.qualifier_for(q2.rooms), "2e2e5e8e1e0e")
    for url, needle in (
            ("https://www.makemytrip.global/hotels/hotel-listing/?city=CTGOI", "not a makemytrip.com"),
            ("https://www.makemytrip.com/flight/search?itinerary=DEL-BOM-15/10/2026", "flights"),
            ("https://www.makemytrip.com/hotels/hotel-listing/?city=CTGOI", "dates"),
            ("https://www.makemytrip.com/hotels/hotel-listing/?checkin=10152026&checkout=10162026"
             "&city=CTGOI&locusType=area", "only city"),
            ("https://www.makemytrip.com/hotels/hotel-listing/?checkin=10152026&checkout=10162026"
             "&city=CTGOI&roomStayQualifier=2e", "roomStayQualifier")):
        q, why = P.query_from_url(url)
        check("refused with the reason: %s" % needle, q is None and needle in (why or ""), why or "")
    equal("a malformed qualifier is None, not a guess", P.rooms_from_qualifier("2e1e"), None)


def check_query_validation():
    import product_parser as P
    today = date(2026, 9, 24)
    base = dict(city="CTGOI", city_code="CTGOI", country_code="IN")
    for kw, needle in (
            ({"checkin": date(2026, 9, 1), "checkout": date(2026, 9, 2)}, "past"),
            ({"checkin": date(2026, 10, 2), "checkout": date(2026, 10, 1)}, "after"),
            ({"checkin": date(2026, 10, 1), "checkout": date(2026, 11, 15)}, "30 nights"),
            ({"checkin": date(2026, 10, 1), "checkout": date(2026, 10, 2), "sort": "stars"}, "--sort"),
            ({"checkin": date(2026, 10, 1), "checkout": date(2026, 10, 2),
              "rooms": (P.Room(adults=0),)}, "--adults")):
        why = P.Query(**base, **kw).validate(today)
        check("refused: %s" % needle, why is not None and needle in why, why or "")
    ok = P.Query(**base, checkin=date(2026, 10, 1), checkout=date(2026, 10, 3)).validate(today)
    equal("a correct query passes", ok, None)
    equal("only the sort keys proven to reorder are offered",
          sorted(P.SORTS), ["popular", "price-asc", "price-desc", "rating"])
    check("the keys the API refused are not among them",
          all((v or {}).get("field") not in ("starRating", "userRating") for v in P.SORTS.values()))


def _cli(**kw):
    base = dict(url=None, city=None, checkin=None, checkout=None, adults=None,
                rooms=None, child_age=None, sort=None, pages=1, concurrency=1)
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Refused(Exception):
    pass


def _build(**kw):
    import page_flow

    def error(msg):
        raise _Refused(msg)
    try:
        return page_flow.build_query(_cli(**kw), error), None
    except _Refused as e:
        return None, str(e)


def check_build_query():
    q, why = _build(city="Goa")
    check("a city NAME is kept for the lookup", q and q.city == "Goa" and q.city_code is None, why or "")
    check("...with dates a month ahead, one night",
          q and q.checkin == date.today() + timedelta(days=30) and q.nights == 1)
    q, _ = _build(city="ctgoi", checkin="2026-12-01")
    equal("a location CODE is taken as the code, checkout the next day",
          (q.city_code, q.country_code, q.checkout), ("CTGOI", "IN", date(2026, 12, 2)))
    q, _ = _build(city="Goa", adults=3, rooms=2, child_age=[4])
    equal("rooms after the first get one adult each",
          [(r.adults, r.child_ages) for r in q.rooms], [(3, (4,)), (1, ())])
    _, why = _build(city="Goa", concurrency=2)
    check("--concurrency 2 is REFUSED, with the cursor as the reason",
          why and "cursor" in why, why or "")
    _, why = _build(url="https://www.makemytrip.com/hotels/hotel-listing/?checkin=10152026"
                        "&checkout=10162026&city=CTGOI", city="Goa")
    check("--url and --city together are refused", why and "--city" in why, why or "")
    q, why = _build(url="https://www.makemytrip.com/hotels/hotel-listing/?checkin=10152026"
                        "&checkout=10162026&city=CTGOI", sort="price-asc")
    check("--sort applies to a --url (the address carries no ordering)",
          q is not None and q.sort == "price-asc", why or "")
    _, why = _build()
    check("no city at all is refused", why and "--city" in why, why or "")
    _, why = _build(city="Goa", checkin="15/10/2026")
    check("a date in the wrong form is refused", why and "YYYY-MM-DD" in why, why or "")


def check_the_request_is_the_front_ends():
    import product_parser as P
    q = _q(sort="price-asc")
    req = P.request_for(q, 1)
    equal("POST to the listing endpoint", (req.method, req.path), ("POST", P.SEARCH_PATH))
    check("on the API host", req.url.startswith("https://mapi.makemytrip.com/"))
    for header in ("tid", "server", "entity-name", "currency", "region", "user-country"):
        check("the front end's %r header rides along (CORS depends on it)" % header,
              header in req.headers)
    equal("the visitor id is the run's own", (req.headers["vid"], req.headers["visitor-id"]),
          (q.visitor_id, q.visitor_id))
    equal("the ordering is the one asked for", req.body["sortCriteria"],
          {"field": "price", "order": "asc"})
    equal("the default ordering sends null, as the site does",
          P.request_for(_q(), 1).body["sortCriteria"], None)
    check("no person's location goes out", "userLocation" not in req.body)
    check("each call has its own request id",
          P.request_for(q, 1).params["requestId"] != P.request_for(q, 1).params["requestId"])


def check_suggestions():
    import product_parser as P
    equal("Goa resolves as the search box does",
          P.pick_suggestion(fx("suggest_goa"), "Goa")[0]["code"], "CTGOI")
    got, _ = P.pick_suggestion(fx("suggest_dubai"), "Dubai")
    equal("Dubai resolves with the SITE's country code", (got["code"], got["country"]),
          ("CTDUB", "UNI"))
    got, why = P.pick_suggestion(fx("suggest_taj_mahal"), "Taj Mahal")
    check("a landmark is refused, naming what the site offered",
          got is None and "Agra" in (why or ""), why or "")
    got, why = P.pick_suggestion(fx("suggest_nothing"), "zzqxw")
    check("nothing matched: refused", got is None and "matched nothing" in (why or ""))
    equal("an unreadable answer is refused, not guessed",
          P.pick_suggestion("<html>", "Goa")[0], None)


# ---------------------------------------------------------------------------
# Page states and markers
# ---------------------------------------------------------------------------

def check_page_states_on_real_captures():
    import html as html_lib
    import product_parser as P
    cases = (("search_goa_p1", 200, "content"), ("search_lehra_nearby", 200, "content"),
             ("search_no_hotels", 200, "empty"), ("search_bad_sort", 200, "rejected"),
             ("akamai_denied_www", 403, "blocked"), ("akamai_denied_www", None, "blocked"),
             ("akamai_denied_api", 403, "blocked"), ("akamai_decoy", None, "blocked"),
             ("akamai_decoy", 200, "blocked"), ("landing_served", 200, "content"),
             ("ssr_listing_goa", 200, "content"), ("suggest_goa", 200, "content"),
             ("chrome_error_page", None, "unknown"))
    for name, status, want in cases:
        equal("%s (HTTP %s) is %s" % (name, status, want),
              P.detect_page_state(fx(name), status), want)
    raw = fx("akamai_denied_www")
    check("the raw refusal is entity-escaped (not vacuous, §20)",
          "errors&#46;edgesuite&#46;net" in raw)
    equal("...and the BROWSER's spelling of it is caught too",
          P.detect_bot_challenge(html_lib.unescape(raw)), "akamai")
    check("Chrome's error page wears the site's hostname as its title (§18)",
          "www.makemytrip.com" in fx("chrome_error_page"))
    equal("an unknown 403 is blocked", P.detect_page_state("<html></html>", 403), "blocked")
    equal("a 429 is throttled", P.detect_page_state("", 429), "throttled")
    equal("the site's own complaint is named", P.api_error(fx("search_bad_sort")),
          "code 400108: field name is not supported")


def check_the_decoy_marker_is_exact():
    import product_parser as P
    check("the decoy is the whole visible text (not vacuous)",
          "200-OK" in fx("akamai_decoy"))
    equal("the raw decoy body", P.detect_page_state("200-OK", 200), "blocked")
    equal("a served page mentioning 200-OK is still served",
          P.detect_page_state(fx("landing_served") + "<p>200-OK</p>", 200), "content")
    for name in ("search_goa_p1", "landing_served", "suggest_goa"):
        check("200-OK is on no served capture (%s)" % name, "200-OK" not in fx(name))


def check_markers_do_not_match_a_page_the_scraping_browser_served():
    """§24: the Scraping Browser's extension injects captcha hunters into
    EVERY page. The marker set must score zero against that injection
    WITHOUT any strip, or the strip is load-bearing."""
    import product_parser as P
    inj = fx("cdp_extension_injection")
    check("the fixture carries the injected hunters (not vacuous)",
          "chrome-extension://" in inj and "turnstile" in inj and "recaptcha" in inj)
    equal("no refusal marker fires on the injection", P.detect_bot_challenge(inj), None)
    equal("a served page WITH the injection is still served",
          P.detect_page_state(fx("landing_served") + inj, 200), "content")
    check("...and no widget marker is in the injection",
          not any(m in inj for m in P.CAPTCHA_WIDGET_MARKERS))
    check("no bare vendor word is a marker (cf-turnstile, captcha, recaptcha)",
          not ({"cf-turnstile", "captcha", "recaptcha", "turnstile"}
               & {m.lower() for m in P.CAPTCHA_WIDGET_MARKERS}))


def check_a_widget_only_counts_on_a_page_the_site_did_not_serve():
    import product_parser as P
    loader = '<script src="https://www.google.com/recaptcha/api.js"></script>'
    equal("a widget loader on a bare page is a challenge",
          P.detect_page_state("<html>%s</html>" % loader, 200), "challenge")
    equal("the same loader on a SERVED page guards nothing (§8)",
          P.detect_page_state(fx("landing_served") + loader, 200), "content")


def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "rejected", "reset",
           "throttled", "unknown"])
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("rejected: not retried, not solved, NOT blocked (a typo is not a proxy problem)",
          not F.should_retry("rejected") and not F.should_solve("rejected")
          and not F.counts_as_blocked("rejected") and not F.should_parse("rejected"))
    check("blocked and reset: retried from a fresh browser, blocked, never solved",
          all(F.should_retry(s) and F.counts_as_blocked(s) and not F.should_solve(s)
              for s in ("blocked", "reset")))
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    check("challenge: solved", F.should_solve("challenge"))
    equal("at most one solve per page", F.SOLVES_PER_PAGE, 1)
    equal("refusals are reported by name",
          [F.refusal_name(s) for s in ("blocked", "reset", "challenge")],
          ["akamai", "akamai-reset", "captcha"])
    check("the advice names the exit that was measured to work",
          "country-in" in F.refusal_advice("reset") and "country-in" in F.refusal_advice("blocked"))
    for text in ("net::ERR_HTTP2_PROTOCOL_ERROR at https://www.makemytrip.com/",
                 "net::ERR_CONNECTION_RESET", "net::ERR_SSL_PROTOCOL_ERROR"):
        check("%s is Akamai hanging up" % text.split("::")[1].split()[0], F.is_reset(text))
    check("a timeout is not a reset", not F.is_reset("Timeout 60000ms exceeded."))
    check("a CDP 401 is explained as expired credentials",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized"))
    check("...and a 500 as a held pid", "pid" in F.cdp_connect_hint("HTTP 500"))
    check("--concurrency 1 is not refused", F.concurrency_refusal(1) is None)


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    src = open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read()
    engines = "".join(open(os.path.join(HERE, m + ".py"), encoding="utf-8").read()
                      for m in ENGINES)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE", "THROTTLE_RETRIES", "THROTTLE_WAIT_S",
                     "FETCH_TIMEOUT_MS", "CORE_FIELD_FLOOR", "RESET_MARKERS",
                     "CDP_CONNECT_ATTEMPTS", "CDP_LOCKED_WAIT_S", "CDP_CONNECT_TIMEOUT_S"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------

class _FakeOps:
    """page_flow's named operations, answering from fixtures."""

    def __init__(self, answers, landing=None, goto_error=None, suggest=None):
        self.answers = answers          # page -> list of (status, text) or error
        self.landing = landing or fx("landing_served")
        self.goto_error = goto_error    # a TransportError text for every goto
        self.suggest = suggest or fx("suggest_goa")
        self.landed = False
        self.pool = None
        self.gotos = self.relaunches = self.solves = 0
        self.fetches = []
        self.bodies = []

    def goto(self, url):
        self.gotos += 1
        if self.goto_error:
            import page_flow
            raise page_flow.TransportError(self.goto_error)
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
        self.bodies.append(req.body)
        if req.page == 0:              # the autosuggest
            return 200, self.suggest, None, None
        queue = self.answers.get(req.page) or [(200, fx("search_no_hotels"))]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, str):      # a fetch() that threw
            return None, "", None, item
        status, text = item
        return status, text, None, None

    def relaunch(self):
        self.relaunches += 1
        self.landed = False

    def proxy_failure(self, text):
        return ""

    def close(self):
        pass


def _run(answers, pages=3, query=None, **extra):
    import page_flow
    ops_kw = {k: extra.pop(k) for k in ("landing", "goto_error", "suggest") if k in extra}
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=1)
        for k, v in extra.items():
            setattr(args, k, v)
        ops = _FakeOps(answers, **ops_kw)
        q = query or _q()
        rc = page_flow.run_pages(lambda: ops, lambda o: None, args, None, q)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = (json.load(open(args.out + ".json"))
                if os.path.exists(args.out + ".json") else None)
    return rc, meta, rows, ops


def check_the_shared_loop_end_to_end():
    ok = {1: [(200, fx("search_goa_p1"))], 2: [(200, fx("search_goa_p2"))]}
    rc, meta, rows, ops = _run(ok, pages=2)
    equal("two served pages: exit 0", rc, 0)
    equal("...complete, stopped because the plan was done",
          (meta["status"], meta["stop_reason"]), ("complete", "completed"))
    equal("...rows in page order", [r["page"] for r in rows], [1, 1, 1, 1, 2, 2, 2])
    check("...page+position unique across the run (§18)",
          len({(r["page"], r["position"]) for r in rows}) == len(rows))
    equal("the browser landed ONCE for the run", ops.gotos, 1)
    sent = ops.bodies[1]["searchCriteria"]
    equal("page 2 was asked for with page 1's cursor",
          sent["lastHotelId"], FIXTURES["search_goa_p1"]["response"]["lastHotelId"])
    equal("the sidecar records the query", meta["query"]["city_code"], "CTGOI")
    equal("...and the currency the site stated", meta["currency"], "INR")

    rc, meta, rows, ops = _run({1: [(200, fx("search_goa_p1"))],
                                2: [(200, fx("search_lehra_nearby"))]}, pages=5)
    equal("the site saying 'no more' ends the run: exit 0", rc, 0)
    equal("...as complete, on the DATA", (meta["status"], meta["stop_reason"]),
          ("complete", "end_of_listing"))
    equal("...pages 3-5 never fetched", ops.fetches, [1, 2])
    equal("...and the substitutes are counted in the sidecar", meta["substitute_rows"], 3)

    rc, meta, rows, ops = _run({1: [(200, fx("search_bad_sort"))]})
    equal("a REJECTED page 1: exit 5, the data never arrived", rc, 5)
    equal("...fetched once, not retried", ops.fetches, [1])
    equal("...and no sidecar beside no output", meta, None)

    rc, meta, rows, ops = _run({1: [(403, fx("akamai_denied_api"))]})
    equal("Akamai's Access Denied on the API: exit 3", rc, 3)
    equal("...tried again from a fresh browser, twice (no pool)", ops.relaunches, 2)

    rc, meta, rows, ops = _run({1: ["TypeError: net::ERR_HTTP2_PROTOCOL_ERROR"]})
    equal("a reset on the fetch: exit 3, a refusal and not a timeout", rc, 3)

    rc, meta, rows, ops = _run({}, goto_error="net::ERR_HTTP2_PROTOCOL_ERROR at "
                                              "https://www.makemytrip.com/hotels/")
    equal("a reset LANDING (every datacentre run): exit 3", rc, 3)
    equal("...and nothing was fetched", ops.fetches, [])

    rc, meta, rows, ops = _run({}, goto_error="Timeout 60000ms exceeded.")
    equal("a landing that timed out: exit 5, not 3 and not 4", rc, 5)

    rc, meta, rows, ops = _run({}, landing=fx("akamai_decoy"))
    equal("the 200-OK decoy on the landing: exit 3", rc, 3)

    rc, meta, rows, ops = _run({1: [(200, fx("search_no_hotels"))]})
    equal("'No Hotels Found': exit 4, and nothing written", (rc, rows), (4, None))

    rc, meta, rows, ops = _run({1: [(429, ""), (200, fx("search_goa_p1"))]}, pages=1)
    equal("a throttle, then the page: exit 0", rc, 0)
    equal("...at the SAME exit (no relaunch)", ops.relaunches, 0)
    rc, meta, rows, ops = _run({1: [(429, ""), (200, fx("search_goa_p1"))]},
                               pages=1, retries=1)
    equal("a throttle wait spends its OWN budget, not --retries (§24): "
          "with --retries 1 the page still arrives", rc, 0)

    dup = {1: [(200, fx("search_goa_p1"))], 2: [(200, fx("search_goa_p1"))]}
    rc, meta, rows, ops = _run(dup, pages=2)
    equal("a property seen on two pages is kept once", len(rows), 4)


def check_a_city_name_is_resolved_before_page_one():
    from product_parser import Query
    q = Query(city="Goa", checkin=date(2026, 10, 15), checkout=date(2026, 10, 16))
    rc, meta, rows, ops = _run({1: [(200, fx("search_goa_p1"))]}, pages=1, query=q)
    equal("the lookup, then page 1", ops.fetches, [0, 1])
    equal("...the code the site's box resolved", q.city_code, "CTGOI")
    equal("...and the run is exit 0", rc, 0)
    q = Query(city="Taj Mahal", checkin=date(2026, 10, 15), checkout=date(2026, 10, 16))
    rc, meta, rows, ops = _run({}, query=q, suggest=fx("suggest_taj_mahal"))
    equal("a name that is no city: exit 2", rc, 2)
    equal("...before any listing page", ops.fetches, [0])
    q = Query(city="Goa", checkin=date(2026, 10, 15), checkout=date(2026, 10, 16))
    rc, meta, rows, ops = _run({}, query=q, goto_error="net::ERR_HTTP2_PROTOCOL_ERROR")
    equal("a lookup that Akamai refused: exit 3, not a usage error", rc, 3)


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
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)


def check_a_remote_relaunch_is_a_new_connection():
    """After a refused connection the Scraping Browser served the reconnect
    from a different exit, so a remote relaunch must reconnect, not only
    clear the landing (as the donor repo's did)."""
    for module in ("playwright_scraper", "puppeteer_scraper"):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        ops_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_Ops")
        relaunch = next(n for n in ops_cls.body
                        if isinstance(n, ast.FunctionDef) and n.name == "relaunch")
        calls = {n.func.attr for n in ast.walk(relaunch)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        check("%s: relaunch opens a new connection" % module, "open" in calls)
        check("%s: ...and there is no early return for a remote browser" % module,
              not any(isinstance(n, ast.Return) for n in ast.walk(relaunch)))


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import Hotel, Product, ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES
    names = [f.name for f in fields(Hotel)]
    equal("the family prefix is byte-identical and in order (§9)", names[:8],
          ["source", "scraped_at", "url", "sku", "title", "price",
           "original_price", "currency"])
    check("the run-describing tail is present",
          {"page", "position", "mode", "data_source", "sort"} <= set(names))
    check("Product is kept as the family's alias", Product is Hotel)
    equal("one mode", sorted(ROW_CLASS_BY_MODE), ["hotels"])
    equal("...one row per sku", list(UNIQUE_BY_SKU_MODES), ["hotels"])
    check("the columns this site gives beyond the family's are there",
          {"price_with_tax", "price_with_fees", "review_source", "section",
           "country_code", "sponsored"} <= set(names))


def check_csv_and_json_writers():
    from output_writer import Hotel, write_csv, write_json
    rows = _rows("search_goa_p1")
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Hotel)
        reader = list(csv.reader(open(csv_path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0],
              [f.name for f in fields(Hotel)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("[") for row in reader[1:] for cell in row))
        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=Hotel)
        equal("an EMPTY csv still carries its header",
              len(list(csv.reader(open(empty_csv, encoding="utf-8")))), 1)
        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        check("a list column stays a real list in JSON", isinstance(loaded[0]["categories"], list))
        check("an 18-digit id stays a string in JSON", isinstance(loaded[0]["sku"], str))


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
    import diff_runs as D
    tracked = D.tracked_fields("hotels")
    check("tracked columns are derived and non-empty", len(tracked) >= 10, repr(tracked))
    check("price and rating are tracked", {"price", "rating", "price_with_tax"} <= set(tracked))
    check("position, url and image are NOT tracked",
          not ({"position", "url", "image", "scraped_at"} & set(tracked)))
    old = [asdict(r) for r in _rows("search_goa_p1")]
    new = copy.deepcopy(old)
    new[0]["price"] = 4000.0
    del new[1]
    result = D.diff_products(old, new)
    equal("one changed", [c["sku"] for c in result["changed"]], [old[0]["sku"]])
    equal("...with the price named", list(result["changed"][0]["changes"]), ["price"])
    equal("one removed", len(result["removed"]), 1)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump(new, open(b, "w"))
        json.dump({"status": "complete", "query": {"checkin": "2026-10-15"}},
                  open(a[:-5] + ".meta.json", "w"))
        json.dump({"status": "complete", "query": {"checkin": "2026-10-16"}},
                  open(b[:-5] + ".meta.json", "w"))
        args = types.SimpleNamespace(old=a, new=b)
        check("two runs for different DATES are refused", not D._check_comparable(args))


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="end_of_listing", pages_requested=3,
                    pages_completed=2, pages_failed=[], products=60,
                    mode="hotels", source="makemytrip.com",
                    start_url="https://www.makemytrip.com/x", final_url="https://www.makemytrip.com/y",
                    extra={"query": {"city_code": "CTGOI"}, "end_of_listing": True})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source", "query", "end_of_listing"):
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
    for module in ENGINES + ("scraper_api_client", "diff_runs", "page_flow", "make_fixtures"):
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


def _ast_flags(module_name):
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


def _engine_flags(module_name):
    """The engine's own flags (read from its source, so no driver is needed)
    plus the shared CLI it calls, built for real rather than read."""
    import page_flow
    source = open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read()
    flags = _ast_flags(module_name)
    m = re.search(r'page_flow\.add_arguments\(p, engine="(\w+)"\)', source)
    if m:
        p = argparse.ArgumentParser()
        page_flow.add_arguments(p, engine=m.group(1))
        flags |= {s for a in p._actions for s in a.option_strings if s.startswith("--")}
    return flags, bool(m)


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
SITE_FLAGS = {"--city", "--checkin", "--checkout", "--adults", "--child-age",
              "--rooms", "--sort"}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both
    ways. The exception list IS the documentation."""
    sets = {}
    for module in ENGINES:
        flags, shared = _engine_flags(module)
        check("%s builds its CLI from page_flow.add_arguments" % module, shared)
        sets[module] = flags
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
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
    """Scoped to the engines and the shared CLI. `--country` is banned: it
    could disagree with the --url, which already names the country."""
    for module in ENGINES + ("page_flow",):
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
    check("the per-site variables carry the MAKEMYTRIP_ prefix",
          {"MAKEMYTRIP_CDP_ENDPOINT", "MAKEMYTRIP_PROXY", "MAKEMYTRIP_URL"} <= read)
    check("the example's endpoint asks for an Indian exit",
          "country-in-pid" in text)


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "MAKEMYTRIP_CDP_ENDPOINT", "MAKEMYTRIP_PROXY"}
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
    import product_parser as P
    q, why = P.query_from_url(values["MAKEMYTRIP_URL"])
    check("the example's MAKEMYTRIP_URL is an address this repo reads", q is not None, why or "")


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
    """A workflow calls ci_checks.py or the CLIs; it does not carry its own
    copy of a check that imports the code (a sibling's first push went red
    on exactly that)."""
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
    """SITE_PUBLIC_IDS forgives a 32-hex as a photo's file name on mmtcdn.com
    and NOTHING else. Planted, not assumed."""
    if not os.path.isdir(os.path.join(HERE, ".github")):
        skip("ci_checks", "no .github directory (the image)")
        return
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks as C
    hexkey = "0123456789abcdef" * 2
    for url in ("https://r1imghtlak.mmtcdn.com/%s.jpeg?output-quality=75" % hexkey,
                "https://r1imghtlak.mmtcdn.com/r2-mmt-htl-image/htl-imgs/"
                "202306071757558261-%s.jpg" % hexkey):
        check("a photo's file name on mmtcdn.com: forgiven (%s)" % url[-24:],
              not C.HEX32.search(C._without_site_ids(url)))
    check("the same value as a field elsewhere: still caught",
          bool(C.HEX32.search(C._without_site_ids('"key": "%s"' % hexkey))))
    check("in a non-image mmtcdn.com path: still caught",
          bool(C.HEX32.search(C._without_site_ids("https://x.mmtcdn.com/api?k=" + hexkey))))
    check("on another host's image: still caught",
          bool(C.HEX32.search(C._without_site_ids("https://example.org/%s.jpg" % hexkey))))


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
    make the same request: the same cookies, the same timeout, and the
    front end's headers, without which the endpoint grants no CORS."""
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        js = re.search(r'FETCH_JS = """(.*?)"""', src, re.S)
        check("%s defines FETCH_JS" % module, js is not None)
        if not js:
            continue
        body = js.group(1)
        for needle in ('credentials: "include"', "AbortController", "Object.assign"):
            check("%s's fetch() carries %s" % (module, needle), needle in body)
        tree = ast.parse(src)
        passes_headers = any(
            isinstance(n, ast.Attribute) and n.attr == "headers"
            and isinstance(n.value, ast.Name) and n.value.id == "req"
            for n in ast.walk(tree))
        check("%s hands req.headers to its fetch()" % module, passes_headers)


def check_selenium_reads_chromes_own_error_page():
    """Selenium reports the REQUESTED address as current_url on Chrome's
    error page, and the page carries more than one ERR_ name."""
    engine = _import_engine("selenium_scraper")
    if engine is None:
        return
    page = fx("chrome_error_page")
    m = engine._CHROME_ERROR_RE.search(page)
    equal("the error named is the page's own errorCode", m and m.group(1),
          "ERR_HTTP2_PROTOCOL_ERROR")
    src = inspect.getsource(engine._chrome_error)
    check("the error page is recognised by the PAGE's location.href",
          "location.href" in src and "current_url" not in src.split('"""')[-1])


def check_pyppeteer_answers_proxy_auth_over_cdp():
    """pyppeteer's page.authenticate relies on Network.setRequestInterception,
    which current Chromium removed: measured, the engine died before its
    first navigation with any credentialled proxy. The engine answers the
    proxy's challenge through the Fetch domain instead, and only a PROXY's."""
    src = open(os.path.join(HERE, "puppeteer_scraper.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("the engine never CALLS page.authenticate", "authenticate" not in called)
    check("...it enables Fetch with handleAuthRequests",
          '"Fetch.enable", {"handleAuthRequests": True' in src)
    check("...answers Fetch.authRequired, continues every paused request",
          "Fetch.authRequired" in src and "Fetch.continueRequest" in src)
    check("...and gives the credentials only to a Proxy challenge",
          'if source == "Proxy"' in src)


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


def check_scraper_api_client():
    """Measured 2026-09-23 against the live Scraper API: a JSON-encoded
    STRING waitFor is answered HTTP 422 and still billed; the target's status
    is `http_code`, while `status` is the API's own verdict. And on this site
    the client reads ONE page's server-rendered listing."""
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
                    "body": fx("akamai_denied_www")}

    def post(url, **kw):
        sent.update(kw.get("json") or {})
        return Resp()

    args = types.SimpleNamespace(url="https://www.makemytrip.com/hotels/hotel-listing/?x=1",
                                 key="k" * 8, timeout=60, cdp_url=None, wait_text="mmtcdn",
                                 wait_element=None, wait_state=None)
    real = sac.requests.post
    sac.requests.post = post
    try:
        _html, status = sac.fetch_html(args)
    finally:
        sac.requests.post = real
    equal("--wait-text sends waitFor as an OBJECT", sent.get("waitFor"), {"text": "mmtcdn"})
    equal("the target status handed onward is http_code", status, 403)
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
    q, why = sac.build_query(types.SimpleNamespace(url_arg=None, city="Goa", checkin=None,
                                                   checkout=None, adults=2))
    check("a city NAME is refused by the client that cannot look it up",
          q is None and "location code" in (why or ""), why or "")
    check("--pages above 1 is refused with the reason",
          "POST endpoint" in inspect.getsource(sac.parse_args))


def check_captcha_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    # Whitespace-normalised: Markdown wraps a sentence wherever it likes,
    # and a phrase split across two lines is still the phrase.
    low = " ".join(readme.lower().split())
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can", "unsolvable captcha"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in low)
    check("the README says flights are not implemented, in those words",
          "does not implement" in low and "flight" in low)


def check_readme_numbers_are_not_stale():
    """§17's check #4: a count claimed in the README is the code's."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import Hotel
    import product_parser as P
    for number in re.findall(r"(\d+)\s+columns", readme):
        equal("the README's '%s columns' is the row class's size" % number,
              int(number), len(fields(Hotel)))
    for number in re.findall(r"(\d+)\s+properties (?:a|per|each) page", readme):
        equal("the README's page size matches the code", int(number), P.ROWS_PER_PAGE)


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
    parser = argparse.ArgumentParser(description="makemytrip-scraper offline suite")
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
