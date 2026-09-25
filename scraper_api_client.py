#!/usr/bin/env python3
"""
makemytrip-scraper — 2captcha Scraper API edition (fourth engine)
=================================================================

A fourth way to run this scraper. Unlike the three browser engines, this one
manages **no browser and no CDP session of its own**: it POSTs a URL to
2captcha's separate **Scraper API** (https://scraper.2captcha.com, a
different product from the Scraping Browser API the other three reach
through --cdp-endpoint), gets the page back over plain HTTPS, and feeds it to
this project's product_parser.

WHAT THIS SITE ALLOWS IT — READ THIS FIRST
------------------------------------------
Two measurements, both 2026-09-24, on one Goa listing URL:

    the Scraper API on its own exits     HTTP 403, Akamai "Access Denied"
    the same, with `cdpurl` set to a
    Scraping Browser profile (country-in) HTTP 200, the full listing page

So on this site the Scraper API works only routed through a Scraping
Browser session (--cdp-url, or MAKEMYTRIP_CDP_ENDPOINT). What it then gets
is a PAGE, and a page carries the listing's first five properties in its
server-rendered state and nothing after them: the rest of the listing is
answered by a POST endpoint, and the Scraper API fetches a URL. So this
client reads **the first five properties of one listing**, and --pages
above 1 is refused with that reason. For more, use a browser engine.

City NAMES are not resolved here either (the lookup is an API call); pass a
listing --url, or --city as the site's location code (CTGOI).

Usage
-----
    python3 scraper_api_client.py --city CTGOI --checkin 2026-11-10

    # the key comes from $TWOCAPTCHA_KEY and the CDP session from
    # $MAKEMYTRIP_CDP_ENDPOINT, so neither needs to be typed — a secret in
    # argv is readable by anything that can run `ps`

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import timedelta
from typing import Optional

import requests

from product_parser import (Query, default_dates, detect_page_state,
                            initial_state_listing, is_location_code,
                            listing_url, parse_date, parse_page,
                            query_from_url)
from output_writer import finish_run, SOURCE_DEFAULT
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` is an OBJECT. Measured 2026-09-23 against the live
    /tasks/sync endpoint: the JSON-encoded string form this client used to
    send was answered with HTTP 422 ("params.waitFor must be an object")
    and was still billed ($0.0005); the same request with an object
    answered HTTP 200. The earlier note here, that the API wanted a
    double-encoded string, no longer describes the API.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # {"status": verdict, "http_code": target status, "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    # The TARGET's HTTP status is `http_code`. `status` is the API's own
    # verdict string ("success"), measured 2026-09-23 -- passing it on
    # handed the page classifier a string, so a target 403/503 was never
    # seen. Fall back to `status` only if it is itself an integer.
    upstream_status = body.get("http_code")
    if not isinstance(upstream_status, int):
        legacy = body.get("status")
        upstream_status = legacy if isinstance(legacy, int) and not isinstance(legacy, bool) else None
    logger.info("Upstream page HTTP status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


def main() -> int:
    args = parse_args()
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2
    query, why = build_query(args)
    if query is None:
        logger.error("%s", why)
        return 2
    if not args.cdp_url:
        logger.warning("No --cdp-url: the Scraper API's own exits were answered "
                       "with Akamai's Access Denied on this site (2026-09-24). "
                       "Set MAKEMYTRIP_CDP_ENDPOINT to route through a Scraping "
                       "Browser session with an Indian exit.")
    args.url = listing_url(query)
    rc, html, status = _fetch_once(args)
    rows, blocked, stop_reason, failed = [], False, "completed", []
    if rc:
        failed, stop_reason = [1], ("blocked_akamai" if rc == 3 else "page_load_timeout")
        blocked = rc == 3
    else:
        state = detect_page_state(html, status, args.url)
        listing = initial_state_listing(html) if state == "content" else None
        if listing is None:
            failed = [1]
            blocked = state == "blocked"
            stop_reason = "blocked_akamai" if blocked else "page_load_timeout"
            logger.error("The page came back %s (upstream HTTP %s)%s.", state,
                         status, "" if state != "content" else
                         " but carried no server-rendered listing")
        else:
            rows = parse_page(listing, query, 1)
            logger.info("Parsed %d propert%s from the page's server-rendered "
                        "listing — the first ones on page 1, which is all a "
                        "fetched PAGE holds.", len(rows), "y" if len(rows) == 1 else "ies")
    return finish_run(rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=1, pages_completed=0 if failed else 1,
                      pages_failed=failed, mode="hotels", source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=args.url,
                      extra={"query": {"city_code": query.city_code,
                                       "country_code": query.country_code,
                                       "checkin": query.checkin.isoformat(),
                                       "checkout": query.checkout.isoformat(),
                                       "rooms": [{"adults": r.adults,
                                                  "child_ages": list(r.child_ages)}
                                                 for r in query.rooms],
                                       "sort": query.sort},
                             "engine": "scraper_api",
                             "server_rendered_only": True})


def build_query(args):
    """(Query, None) or (None, reason), from --url or from the flags."""
    if args.url_arg:
        return query_from_url(args.url_arg)
    city = (args.city or "").upper()
    if not is_location_code(city):
        return None, ("--city must be the site's location code here (CTGOI for "
                      "Goa): resolving a name is an API call this client "
                      "cannot make. Or pass a listing --url.")
    checkin = parse_date(args.checkin) if args.checkin else default_dates()[0]
    if checkin is None:
        return None, "--checkin %r is not a date (YYYY-MM-DD)" % args.checkin
    checkout = parse_date(args.checkout) if args.checkout else checkin + timedelta(days=1)
    if checkout is None:
        return None, "--checkout %r is not a date (YYYY-MM-DD)" % args.checkout
    from product_parser import Room
    q = Query(city=city, city_code=city, country_code="IN", checkin=checkin,
              checkout=checkout, rooms=(Room(adults=args.adults),))
    why = q.validate()
    return (None, why) if why else (q, None)


def _fetch_once(args):
    """(rc, html, upstream_status). rc is 0 on a fetch that returned,
    EXIT_API_ERROR on the API failing, 3 on Akamai's refusal after the
    retries."""
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            body, upstream_status = fetch_html(args)
        except requests.RequestException as e:
            logger.error("Network error talking to the Scraper API: %s",
                         _redact_debug_header(str(e)))
            return EXIT_API_ERROR, "", None
        except RuntimeError as e:
            logger.error("%s", _redact_debug_header(str(e)))
            return EXIT_API_ERROR, "", None
        if args.dump_html:
            with open(args.dump_html, "w", encoding="utf-8") as f:
                f.write(body)
            logger.info("Response written to %s", args.dump_html)
        if detect_page_state(body, upstream_status, args.url) != "blocked":
            return 0, body, upstream_status
        if attempt < attempts:
            logger.info("Akamai refused attempt %d/%d — retrying in %ds.",
                        attempt, attempts, args.retry_delay)
            time.sleep(args.retry_delay)
    logger.error("The Scraper API's fetch was refused by Akamai on every "
                 "attempt. Route it through a Scraping Browser session with "
                 "an Indian exit (--cdp-url), or use a browser engine.")
    return 3, "", None


def parse_args():
    p = argparse.ArgumentParser(
        description="makemytrip.com hotel listing — 2captcha Scraper API "
                    "edition (no local browser). Reads the first properties "
                    "of ONE listing page, from its server-rendered state.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var (a key in argv is
    # visible to anyone who can run `ps`).
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", dest="url_arg", default=None,
                   help="A hotel listing address (…/hotels/hotel-listing/?city=…).")
    p.add_argument("--city", default=None,
                   help="The site's location code (CTGOI). Names are not "
                        "resolved by this client.")
    p.add_argument("--checkin", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--checkout", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--adults", type=int, default=2)
    p.add_argument("--pages", type=int, default=1,
                   help="Must be 1: a fetched page holds the listing's first "
                        "properties only, and the rest is a POST.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="makemytrip_hotels_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=90,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 90)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port. "
                        "Needed on this site; also read from MAKEMYTRIP_CDP_ENDPOINT.")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible.")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if Akamai refuses. Each attempt is a "
                        "separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the page to this path, even on success")
    args = p.parse_args()
    args.url = None
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "MAKEMYTRIP_CDP_ENDPOINT": "cdp_url",
    })
    if args.pages != 1:
        p.error("--pages must be 1 for this client: a fetched page holds the "
                "listing's first properties only, and everything after them is "
                "answered by a POST endpoint the Scraper API cannot call. Use a "
                "browser engine for more.")
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
