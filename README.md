# makemytrip-scraper

[![release](https://img.shields.io/github/v/release/2scraper/makemytrip-scraper)](https://github.com/2scraper/makemytrip-scraper/releases)
[![tests](https://github.com/2scraper/makemytrip-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/makemytrip-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/makemytrip-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/makemytrip-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![account](https://img.shields.io/badge/makemytrip%20account-not%20needed-brightgreen)

Scrapes [makemytrip.com](https://www.makemytrip.com)'s **hotel listings**
into JSON and CSV: one row per property on a city's listing, for the dates
and guests you give it.

| column group | what you get |
|---|---|
| price | per night: after the site's discount, with taxes, and with the fees collected at the property; the struck-through price and the discount it implies; the coupon the site applied |
| rating | the guest rating, its rating and review counts, the site's label ("Very Good"), and **whose reviews** it summarises (`MMT`, `MMT_EXP`, `MMT_BKG`, ...) |
| property | name, type (Hotel, Resort, Hostel, Apartment, ...), stars, locality, coordinates, the site's categories, sold-out and sponsored flags |
| provenance | which block of the listing it came from (see trap 1), the dates, guests and ordering the run asked for, page and position |

41 columns in all, the same in JSON and CSV. A `<out>.meta.json` beside the
output records the query, the currency, and whether the site said the
listing had ended.

Flights are a separate product on the site, and **this repo does not
implement flights**: their results arrive as a server-sent event stream,
and every attempt to read it through the Scraping Browser failed
(`net::ERR_FAILED`, 3 of 3 on 2026-09-24). That is a TODO, not a statement
about the site.

---

## Start with the part most scrapers bury

**From a datacentre address, makemytrip.com serves nothing to anyone, and
there is no captcha to solve on the way in.** Measured 2026-09-24 from a
datacentre VPS (netcup, AS197540):

| what was asked | answer |
|---|---|
| any page, plain curl | HTTP 403, Akamai "Access Denied" |
| the same with a Chrome User-Agent | the connection is reset, or hangs |
| real Chromium, headless **and** headful | `net::ERR_HTTP2_PROTOCOL_ERROR`, every page |
| Chrome under Selenium with its User-Agent overridden | HTTP 200 and a body of `200-OK`: a decoy, not the page |
| the listing API itself (`mapi.makemytrip.com`) | HTTP 403, Akamai "Access Denied" |
| three 2Captcha residential exits (eu, na, and one with `-region-in`) | `net::ERR_HTTP2_PROTOCOL_ERROR` |
| the 2Captcha Scraper API on its own exits | HTTP 403, Akamai "Access Denied" |
| **the Scraping Browser with an Indian exit (`country-in`)** | **HTTP 200, the full listing** |

So what changes Akamai's answer here is the ADDRESS, not the client: real
Chromium is refused as hard as curl, and served from India. Every page
captured was counted for captcha markers after removing what the Scraping
Browser's own extension injects: 0 reCAPTCHA, 0 hCaptcha, 0 Turnstile,
0 sitekeys. Akamai refuses by hanging up, and there is nothing on the page
for any solver to work on.

What was measured to work is **one product: the Scraping Browser API with a
`country-in` profile.** 4 of 6 fresh connections were served on the first
try; the other two (a reset and a timeout) were cleared by reconnecting, and
the service served the reconnect from a different Indian exit. The engines
do that reconnect by themselves.

The residential proxies above are recorded as **not measured to work**, not
as broken: whether it was those exits or this machine's own Chromium being
scored was not separated. A home connection in India has not been tried.

---

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
cp .env.example .env     # then put a Scraping Browser endpoint in it
```

`playwright install chromium` is only needed for a local browser, which a
datacentre cannot use here (above). Install **one** engine per virtualenv:
the three libraries pin versions of their dependencies that cannot all be
satisfied at once.

---

## Run

```bash
# Goa, two nights, the first three pages (30 properties a page)
./venv/bin/python playwright_scraper.py --city Goa \
    --checkin 2026-11-10 --checkout 2026-11-12 --pages 3

# Dubai, cheapest first, one adult
./venv/bin/python playwright_scraper.py --city Dubai --sort price-asc --adults 1

# a family: two adults and two children aged 5 and 8, in one room
./venv/bin/python playwright_scraper.py --city Mumbai --child-age 5 --child-age 8

# or read the query from the site's own listing address
./venv/bin/python playwright_scraper.py --url "https://www.makemytrip.com/hotels/hotel-listing/?checkin=11102026&checkout=11112026&city=CTGOI&locusId=CTGOI&locusType=city&country=IN&roomStayQualifier=2e0e"
```

`--city` takes a **name**, looked up through the site's own search box, or
the site's location code (`CTGOI`). With no dates, a run searches one night
a month ahead. `--sort` is `popular` (the site's default), `price-asc`,
`price-desc` or `rating`: each was checked to change the order, and the
site's API refuses `starRating` and `userRating` outright.

Measured through a `country-in` Scraping Browser profile, 2026-09-24:

| run | engine | rows | notes |
|---|---|---|---|
| Goa, 3 pages | Playwright | 90 of 90 priced | 85 rated; 19 with no stars (hostels, homestays); 0 sponsored |
| Dubai by name, 2 pages, 3 nights | Playwright | 60 of 60 priced | `Dubai` -> `CTDUB`, country `UNI`; 51 of 60 carry a fee paid at the property |
| Mumbai, `price-asc`, 2 pages | pyppeteer | 56 of 60 kept | 4 duplicates across the page boundary (trap 4) |
| Goa `--url`, `--sort rating` | Playwright | 30 | 4.9, 5.0, 4.9, 4.8, 4.8 at the top |
| Goa, one page | Scraper API + `cdpurl` | 5 | the page's server-rendered five; $0.0005 |

---

## Five things about MakeMyTrip that will look like bugs

### 1. A wrong city code returns another town's hotels

`cityCode=CTLEH` is not Leh. The site answered it as "Lehra" and returned 7
properties in Patran and elsewhere, under a block it calls `NEARBY_HOTELS`
with the heading "No more properties in Lehra matching your search", in an
HTTP 200 that looks like any other page. Every row therefore carries
`section`: `RECOMMENDED_HOTELS` for the city you asked for, anything else
for the site's substitutes, and the run warns and counts them in the
sidecar. Pass `--city` by **name** where you can: the site's own search box
resolves it.

### 2. Prices are per night, in INR, whatever the city

`price` is per night before taxes; a 3-night search returned the same
per-night figures as a 1-night one. The currency follows the site's
REGION, so Dubai hotels come back in INR, with the city tax stated
separately in AED and folded into `price_with_fees`. Where a property has
no struck-through price, `original_price` and `discount_pct` are null, not
zero.

### 3. The site's country codes are not ISO

The United Arab Emirates is `UNI` in this site's data, and that is what
`country_code` carries. Looking a city up by name supplies the right one;
a bare code assumes India and the run warns if the site disagrees.

### 4. Pages can overlap, and the listing states no total

The listing is paged by a cursor each response hands to the next request,
so pages are fetched one after another and `--concurrency` above 1 is
refused. Under `price-asc`, properties at the same price straddle the
cursor: 4 of 30 on Mumbai's page 2 were repeats, and they are dropped and
logged. Whether a property can also fall BETWEEN two pages is not visible
from outside. The site gives no total up front, and a page may hold fewer
than 30 (18 on one Goa page 1); a run ends early, and complete, when the
site says nothing follows.

### 5. Zero stars and a zero rating are "none"

Hostels, homestays and villas carry `starRating: 0`, and an unrated
property carries a rating of 0 with 0 ratings. Both become null, rating and
counts together, so an average over the column means something.

---

## Engines

| | |
|---|---|
| `playwright_scraper.py` | **Primary.** Authenticates a proxy and a remote CDP endpoint. Run live, above. |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained; here for parity. Run live through the Scraping Browser, above. `--chromium-path` points it at another browser. A credentialled `--proxy` is known NOT to authenticate through it on current Chromium (`page.authenticate` relies on a CDP method Chromium removed; found in a sibling repo, 2026-09-24) and is not fixed here, since no proxy was measured to help on this site anyway. |
| `selenium_scraper.py` | Drives a Chrome you have. **Cannot authenticate a remote CDP endpoint** (`debuggerAddress` is a bare `host:port`), so it refuses the Scraping Browser with exit 2, and that is the one path measured to work. From a datacentre it is therefore refused on every run, and reports it as exit 3: verified live against the `200-OK` decoy this engine's Chrome is served, and against a captured copy of Chrome's own error page, which Selenium reports under the REQUESTED address. A successful Selenium run has not been measured. |
| `scraper_api_client.py` | No local browser: the 2Captcha Scraper API fetches the listing PAGE, routed through a Scraping Browser session (`cdpurl`), and this client reads the page's server-rendered state. That is **the listing's first five properties, and nothing more**: everything after them is a POST endpoint, and the Scraper API fetches URLs. `--pages` above 1 is refused for that reason. |

The fetch loop itself (landing, retries, reconnecting on a refusal, parsing)
is one implementation in `page_flow.py` that all three browser engines
drive, so they cannot disagree about a page.

---

## What the 2Captcha products buy, and when

One key, four separately-billed products ([2captcha.com](https://2captcha.com)):

* **The Scraping Browser API** (`--cdp-endpoint`, `MAKEMYTRIP_CDP_ENDPOINT`)
  is what makes this repo work from a datacentre: a remote browser with an
  Indian exit. One live connection per `pid`. A profile's credentials last
  about a day; an expired one answers HTTP 401, which the engines report as
  exit 5 naming the expiry.
* **The Scraper API** (`scraper_api_client.py`) works here only routed
  through such a session, and then reads five properties per listing.
* **Captcha solving**: no captcha was met on this site. The family's
  reCAPTCHA detection is wired in, so a widget that ever appears in front of
  the landing is solved rather than reported as a timeout, and over the
  Scraping Browser its own auto-solve gets the first turn. That path has had
  nothing to solve here.
* **Proxies** (`--proxy`, `--proxy-file`) and **fingerprints**
  (`--fingerprint`) are supported by the engines and **not measured to help
  on this site** (see the table at the top).

Nothing here integrates a competitor.

---

## Exit codes

| | |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage, including a city name the site matches to no city |
| 3 | blocked: Akamai's Access Denied, its dropped connection, or its `200-OK` decoy |
| 4 | zero rows: the site answered "No Hotels Found" |
| 5 | the data never arrived: a timeout, a refused parameter, an expired or locked Scraping Browser profile |
| 6 | partial: some pages came back and some did not |

**A run that finds nothing writes nothing**, so a failure never replaces last
night's good output with `[]`. `--allow-empty` is the opt-out.

`diff_runs.py --old a.json --new b.json` compares two runs of the same query
by `sku`: new and vanished properties, and every tracked column that
changed. It refuses two runs for different dates, guests or orderings, since
a hotel's price is a price for one stay.

---

## Configuration

Credentials live in `.env` next to the scripts, never on a command line.
Copy [`.env.example`](.env.example) and fill in what you use;
`python3 env_config.py` prints what was picked up **without printing
secrets**. Precedence: explicit flag → exported environment variable →
`.env` → default.

---

## Tests

```bash
python3 smoke_test.py          # offline, no network, no engine needed
python3 smoke_test.py -v       # every check as it passes
pytest                          # the same suite, one test
```

The fixtures are real responses, trimmed and scrubbed by `make_fixtures.py`,
which proves each one parses identically to its original. The suite drives
the shared fetch loop end to end with a fake browser: a full listing, the
site's own end, a refused parameter, each of Akamai's three refusals, a
landing that timed out, an empty listing, a throttle and a city name that is
no city.

The [canary](.github/workflows/canary.yml) runs twice over. Daily, with no
secrets, from a GitHub runner: a datacentre, so it asserts the refusal is
REPORTED as one (exit 3, never a crash or an "empty listing"). On dispatch,
through a Scraping Browser profile: a real 3-page Goa scrape, checked for
prices, currency, duplicates and zero ratings.

---

## Legal

This reads **public data**: the hotel listing the site shows an anonymous
visitor, fetched as its own pages fetch it. It books nothing, holds no
inventory and reads nothing behind a login.

Rate limits, terms of service and the legality of scraping in your
jurisdiction are your responsibility as the operator. `--delay` defaults to
1 second between pages.
