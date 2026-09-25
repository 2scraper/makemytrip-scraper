# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

MakeMyTrip changing its listing API is the normal way this stops working, and
it has its own issue template. The parser reads no HTML for its rows: every
row comes out of one JSON endpoint the site's own front end calls,
`POST mapi.makemytrip.com/clientbackend/cg/search-hotels/DESKTOP/2`
(`product_parser.py` documents it). So there are only four things that can
break, and each is loud or guarded:

1. **The request body.** The endpoint answers a field it no longer accepts
   with HTTP 200 and an error code (400108 "field name is not supported", for
   one). That is classified `rejected`, the run stops naming the site's own
   complaint with exit 5, and it is not retried or counted as blocked.
2. **The experiment string.** Without the front end's `expData` in the body
   (`product_parser.EXP_DATA`) the API still answers, with hotels and NO
   prices. That is the quiet one; the price-coverage floor in
   `page_flow.CORE_FIELDS` and the canary are what catch it. If prices go
   null on every row, copy the current value out of the site's own request.
3. **A record's own field names** (`name`, `priceDetail`, `starRating`,
   `geoLocation`, the cursor's `lastHotelId` and `lastFetchedWindowInfo`,
   ...). The row still writes, with that column null, or pagination stops
   after page 1. `page_flow.CORE_FIELDS` is the guard, a coverage floor of
   99% on the columns every captured record carried.
4. **Akamai changing what it refuses.** Everything on the site is already
   behind Akamai Bot Manager, and what was measured to get through
   (2026-09-24) is the Scraping Browser API with a `country-in` profile. If
   that stops being served, the refusal is reported as exit 3 with
   `blocked_akamai` or `blocked_akamai-reset`, and the dispatch-only canary
   job says so.

If you are reporting a break, say which of those four it is, and attach the
`--dump-html` output: the exact JSON the parser was given, on success as well
as failure.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once,
   both halves. The daily job needs no secrets: it runs from a GitHub runner
   (a datacentre address) and asserts that Akamai's refusal is reported as
   exit 3, not as an empty or a crashed run, and if the runner is ever
   served it checks the rows strictly instead. The second job is
   dispatch-only and goes through a Scraping Browser profile (the
   `MAKEMYTRIP_CDP_ENDPOINT` secret) to check a 3-page Goa run. A profile's
   credentials last about a day, so refresh the secret before dispatching.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions. Its fixtures are real API responses, trimmed, in
`fixtures_generated.json`, which `make_fixtures.py` regenerates from a
capture directory. Copy the nearest existing check and edit it.

Six properties in this repo exist because they were measured against
expectation and cost real time. Tests pin all six, so a PR that breaks one
fails rather than silently regressing:

- **A wrong city code returns ANOTHER town's hotels.** `cityCode=CTLEH` was
  answered as "Lehra", with 7 properties in Patran under a section the site
  calls `NEARBY_HOTELS`, in an HTTP 200 that looks like any other page. Rows
  therefore carry `section`, the run warns when a page is nothing but
  substitutes, and `--city` is resolved by name through the site's own
  autosuggest. A name it matches to no city is exit 2.
- **Prices are per night in INR, whatever the city.** Dubai comes back in
  INR, with the city tax stated in AED and folded into `price_with_fees`.
  The currency is what the site states; nothing converts it.
- **The site's country codes are not ISO.** The UAE is `UNI`, and that is
  what `country_code` carries.
- **The listing is paged by a cursor** (`lastHotelId` plus
  `lastFetchedWindowInfo`), so page N cannot be requested before page N-1
  and `--concurrency` above 1 is refused, not clamped. Under
  `--sort price-asc` pages overlap at the cursor: 4 of 30 on Mumbai's page 2
  were repeats (2026-09-24). The dedupe drops them and the log says so.
- **Zero is not a rating.** `starRating: 0` (hostels, homestays) and a
  rating of 0 with 0 ratings both become null, counts together.
- **A refused parameter is `rejected`, not blocked**, and "No Hotels Found"
  (error 400814) is `empty`, exit 4. Classified as a block, either would
  send a reader to buy an exit for a typo.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a city with genuinely
  nothing for those dates, which is a correct answer — `5` the data never
  arrived (a timeout, a refused parameter, an expired or locked Scraping
  Browser profile), `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so a retried page cannot
  change the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier and
the CLI contract against real, trimmed responses. If yours genuinely needs
makemytrip.com, say in the PR what you ran (engine, city, dates, sort), from
which exit, and what you got, including the sidecar's `stop_reason`.

Two things about running this live that are specific to MakeMyTrip:

* **A datacentre address is refused whatever the client.** Measured
  2026-09-24: curl got Akamai's "Access Denied", real Chromium (headless and
  headful) got `net::ERR_HTTP2_PROTOCOL_ERROR`, Selenium's Chrome got an
  HTTP 200 whose body is `200-OK` (a decoy), and the Scraper API on its own
  exits got 403. Three 2Captcha residential exits also got the protocol
  error; that is recorded as not measured to work, not as broken. What was
  served is the Scraping Browser API with a `country-in` profile
  (`--cdp-endpoint`, or `MAKEMYTRIP_CDP_ENDPOINT` in `.env`): 4 of 6 fresh
  connections first try, the other two cleared by reconnecting.
* **Selenium cannot use that path**, because chromedriver cannot
  authenticate a remote CDP endpoint. So a successful Selenium run has not
  been measured; a PR touching `selenium_scraper.py` should say what it was
  verified against.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own, and only running it proves it.

## Scope

This repo reads **public data** on makemytrip.com: the hotel listing for a
city, dates and guests, exactly as the site's own front end fetches it for
an anonymous visitor.

Out of scope: anything behind a login, anything that books, holds or pays
for a stay or submits any other form, and anything that defeats a
protection rather than passing it the way an ordinary browser does. Flights
are not implemented: their results are a server-sent event stream that
failed through the Scraping Browser 3 of 3 times (2026-09-24).

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
