# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

> **Behaviour changes.**
>
> * `--retries 0` is now a usage error (exit 2). It used to be accepted, and
>   the page loop (`while attempt < args.retries`, after the landing) then
>   never sent the listing request: the run fetched no page and ended with
>   no rows. If a script of yours passes 0, pass **1**: the flag counts
>   ATTEMPTS, so 1 means "try once and do not retry".
> * A page the site SERVED with properties in it that parses to no rows is
>   now a failed page, `stop_reason: parser_found_nothing`: exit 5 when
>   nothing was gathered, exit 6 (`partial`) after earlier pages. It used to
>   be exit 4 ("the listing is empty") on page 1, and a complete run with
>   exit 0 ("the listing ended") on a later page.
> * Negative values for `--delay`, `--retry-delay` and
>   `--proxy-block-retries` are usage errors (exit 2) naming the flag.
> * Output files (`.json`, `.csv`, `.meta.json`) are written to a temporary
>   file beside the target and renamed over it. A new file gets the mode
>   `open()` would have given it (`0o666 & ~umask`); a file that already
>   exists keeps its own mode.
> * CSV cells that begin with `=`, `+`, `-`, `@`, a tab, CR or LF get a
>   leading apostrophe, so a spreadsheet reads them as text. JSON is
>   unchanged, and the sidecar records `csv_cells_escaped`.
> * `.env.example` ships `MAKEMYTRIP_URL` commented out, and a URL from the
>   environment no longer blocks typed query flags.

### Fixed

- **`--retries 0` fetched nothing and called it a result.** `--retries` now
  goes through `page_flow.attempts` in every entry point; a check reads the
  source of each one and calls the validator, so a new engine declaring the
  flag with a plain int fails the suite.
- **A served page whose rows the parser lost read as an empty or a finished
  listing.** With every hotel's `id` renamed in a real Goa capture, the page
  still classified as content holding 4 properties and parsed to 0 rows;
  `--pages 1` exited 4, and a good page 1 followed by such a page 2 exited 0
  with `status: complete`. Both are now parser failures (see above), and the
  response is saved as `<out>_pageN_debug.html` for the issue.
- **The README's first run failed after `cp .env.example .env`.** The
  example shipped a real `MAKEMYTRIP_URL`, the engines applied `.env` before
  checking for a `--url`/`--city` conflict, and `--city Goa` exited 2 with
  "--url already carries the query". The line is now commented out, each
  engine records whether `--url` was typed before `.env` is applied, and an
  environment URL gives way to typed query flags. A typed `--url` with
  `--city` is still refused.
- **One page could buy three captcha solves, and a run by city name five,**
  against `SOLVES_PER_PAGE = 1`: the allowance restarted on every landing,
  and a refused page lands again after each relaunch. Measured with the
  solver stubbed; real spend also needs `TWOCAPTCHA_KEY` and a captcha this
  site has not been seen to serve. Every landing for a page now spends one
  `page_flow.SolveBudget`, checked before each purchase and never reset by a
  retry or a rotation, and the city lookup spends page 1's.
- **A write that failed halfway destroyed the previous good output.** A
  failure injected inside `json.dump` left `out.json` and `out.meta.json`
  as 10-byte fragments of invalid JSON. Writes are now atomic per file.
- **Formula-shaped text from listings reached CSV cells unescaped** (see
  above).
- **A CDP 401 was always explained as credentials that "last about a
  day".** That lifetime was not measured and is not a constant. The message,
  README, TROUBLESHOOTING, `.env.example`, the bug template and the canary
  now say the endpoint's credentials were refused (expired, revoked or
  mistyped).
- `.dockerignore` named another repo's output prefix; it now lists this
  repo's (`makemytrip_hotels.*`), `.env.*`, canary outputs and `.pageN`
  dumps.

### Changed

- **The canary's Scraping Browser job runs on the schedule** instead of on
  dispatch only, and FAILS with an error naming `MAKEMYTRIP_CDP_ENDPOINT`
  when that secret is missing, instead of skipping green. The datacentre
  job still expects Akamai's refusal (exit 3) and says it does not test
  extraction; the Scraping Browser job is the one that requires real rows.
- Comments, docs and the suite state their reasons in this repo's own words
  instead of pointing at section numbers in notes the public cannot read. A
  suite check keeps it that way.
- The README's `engines` badge no longer lists the three engines as equals:
  it says pyppeteer is measured over CDP and Selenium locally only, which is
  what the engine table below it already said.
- The README dates the last live checks beside the measurements table.

### Added

- `.github/env_report.py`, run by both canary jobs before the scrape: it
  prints the Python, TLS library, driver and (for the local-browser job)
  Chromium versions, and the kind of endpoint, never the endpoint. A red
  canary then says from its own log whether the site or the runner moved.
  Prompted by a third-party audit that met a TLS-library warning on an
  older Python and could not tell it from the block.

## [0.1.1] — 2026-09-25

### Fixed

- **pyppeteer with a credentialled `--proxy` died before its first
  navigation**, with "'Network.setRequestInterception' wasn't found":
  pyppeteer's `page.authenticate` relies on a CDP method current Chromium
  removed. The engine now answers the proxy's challenge itself through the
  `Fetch` domain, giving the credentials only to a proxy's challenge.
  Measured after the fix: ipify answered through the proxy on 4 of 4 runs.

### Measured

- Through pyppeteer, the 2Captcha residential exits got makemytrip.com's
  page on 2 of 3 tries, and then the listing API refused the exit (its
  answer carried no CORS permission). So the proxy path still returns no
  rows, and the README and `.env.example` say so in those terms. The
  Scraping Browser with `country-in` remains the one path measured to work.
- The canary's first runs from a GitHub runner: refused by Akamai and
  reported as exit 3, as the README says of a datacentre; and through a
  Scraping Browser profile, 90 of 90 rows priced over three pages.

## [0.1.0] — 2026-09-24

First release. makemytrip.com's hotel listings, read from the site's own
listing API, through three browser engines over one shared fetch loop, and
the 2Captcha Scraper API for the first five properties of a listing page.

> **Read before running from a server.** From a datacentre address the site
> served nothing to any client in testing, real Chromium included, and no
> captcha was involved: Akamai drops the connection. The one path measured
> to work is the Scraping Browser API with a `country-in` profile
> (`--cdp-endpoint`, or `MAKEMYTRIP_CDP_ENDPOINT` in `.env`).

### Added

- `--mode hotels` (the only mode): one row per property on a city's hotel
  listing, 41 columns: the price per night after the site's discount, with
  taxes, and with the fees paid at the property; the struck-through price
  and the discount computed from it; the coupon the site applied; stars, the
  guest rating with its counts and its SOURCE; property type, locality,
  coordinates, categories, sold-out and sponsored flags; the listing block
  it came from; and the query it was read for.
- `--city` by name, resolved through the site's own autosuggest (what its
  search box does), or by the site's location code. `--checkin`,
  `--checkout`, `--adults`, `--child-age`, `--rooms`, and `--sort`
  (`popular`, `price-asc`, `price-desc`, `rating`: the four orderings
  measured to change the list).
- `--url` reads the query from the site's own hotel-listing address.
- The listing is paged by the server's cursor, one page after another;
  `--concurrency` above 1 is refused with that reason. A run ends early, and
  complete, when the site says nothing follows.
- Akamai's three refusals are exit 3 in all three engines: its "Access
  Denied" page (matched in both the raw and the browser spelling), its
  dropped connection (`ERR_HTTP2_PROTOCOL_ERROR` and relatives), and an
  HTTP 200 whose whole body is `200-OK`. A refused connection is retried
  from a fresh browser, which over the Scraping Browser is a fresh exit.
- `scraper_api_client.py`: the Scraper API, routed through a Scraping
  Browser session, reads the first five properties a listing page renders
  on the server. `--pages` above 1 is refused: everything after them is a
  POST.
- `diff_runs.py` compares two runs of the same query by `sku`, and refuses
  runs for different dates, guests or orderings.
- A canary in two parts: daily with no secrets from a GitHub runner,
  asserting a datacentre refusal is reported as exit 3; and on dispatch,
  through a Scraping Browser profile, a real three-page Goa scrape.

### Not implemented

- Flights. Their results arrive as a server-sent event stream, which failed
  through the Scraping Browser on every attempt (3 of 3). This repo does not
  implement them.
- Locality, area and hotel-chain listings (`locusType` other than `city`).
