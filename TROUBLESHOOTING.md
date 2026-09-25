# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

## Exit 2 — bad usage

* **"'X' matched no city on makemytrip.com; the site suggested ..."** — the
  name went through the site's own autosuggest and no CITY came back (a
  hotel or an area can). Use one of the suggestions, or the site's location
  code (`CTGOI`). A name is safer than a code you half-know: see "Rows from
  another town" below.
* **"--url already carries the query"** — pass a URL or `--city` and the
  other query flags, not both. Merging them could scrape something neither
  named.
* **"--concurrency N is refused"** — the listing is paged by a cursor that
  each response hands to the next request, so page N cannot be fetched
  before page N-1. Run several cities in parallel, one process each.

## Exit 3 — blocked

The log names what refused the run:

* **`akamai`** — Akamai Bot Manager's "Access Denied" (HTTP 403), or the
  HTTP 200 whose whole body is `200-OK`, a decoy that Chrome under Selenium
  was served. From a datacentre address this is the expected answer
  (measured 2026-09-24).
* **`akamai-reset`** — Akamai dropped the connection instead of answering
  (`net::ERR_HTTP2_PROTOCOL_ERROR` or a reset). From a datacentre it was
  every answer to real Chromium, headless and headful, on 2026-09-24.
* **`captcha`** — a captcha stood in front of the landing. Not observed on
  this site: no captcha was met on any served page. If you see it, please
  report it.

What was measured to change the answer is the ADDRESS, not the client: the
Scraping Browser API with a `country-in` profile (`--cdp-endpoint`, or
`MAKEMYTRIP_CDP_ENDPOINT` in `.env`). Through it 4 of 6 fresh connections
were served first try on 2026-09-24, and the engines reconnect by
themselves when one is refused. Three 2Captcha residential exits (eu, na,
`-region-in`) were not measured to work. Selenium cannot use the Scraping
Browser, so it is refused from a datacentre on every run.

A `<out>_page<N>_debug.html` beside the output holds what came back.

## Exit 4 — zero rows

The site answered "No Hotels Found" (error 400814): a city with nothing for
those dates and guests, or a code it does not know. Nothing is written, so
an earlier good file is left alone; `--allow-empty` writes the empty file.

## Exit 5 — the data never arrived

* **"The endpoint refused this request (...)"** — the site rejected the
  parameters, for example error 400108 "field name is not supported". The
  same request would be rejected again, so it is not retried. If the query
  looks right, the API changed: open a "Site changed" issue with
  `--dump-html` output.
* **"HTTP 401: the endpoint's credentials were refused"** — the Scraping
  Browser profile has expired. Its credentials last about a day; get a
  fresh endpoint from your 2Captcha dashboard.
* **HTTP 500 / `profile_locked`** — another run still holds this `pid`. A
  profile allows one live connection; wait for the other run, or use a
  different pid.
* **"Gave up on page N"** — a timeout or a dead proxy. The log names which.

## Exit 6 — partial

Some pages came back and a later one did not. The output holds what was
gathered and the sidecar lists `pages_failed` by number.

## A run looks fine but a column is wrong

Re-run with `--dump-html response.json`: it writes the exact JSON the parser
was given, on success too, so a parsing bug can be told apart from a change
in what the site sends. If every price is null, the front end's experiment
string (`product_parser.EXP_DATA`) has probably changed: without it the API
returns hotels with no prices.

## Rows from another town

A wrong or half-known city code is answered with ANOTHER place's hotels, in
an HTTP 200: `CTLEH` came back as "Lehra", with properties in Patran. Those
rows carry a `section` other than `RECOMMENDED_HOTELS`, and the run warns
and counts them in the sidecar. Pass `--city` by name.

## Prices in INR for a city abroad

That is the site: prices are per night, in INR, whatever the city. Dubai's
city tax is stated in AED and folded into `price_with_fees`. Country codes
are the site's own, not ISO (the UAE is `UNI`).

## Duplicates dropped between two pages

Under `--sort price-asc`, properties at the same price straddle the cursor,
so a page can repeat rows from the one before (4 of 30 on Mumbai's page 2,
2026-09-24). The dedupe drops them and the log says so. That is the site's
paging, not the scraper.
