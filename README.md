# binance-scraper

[![release](https://img.shields.io/github/v/release/2scraper/binance-scraper)](https://github.com/2scraper/binance-scraper/releases)
[![tests](https://github.com/2scraper/binance-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/binance-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/binance-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/binance-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![runs without an account](https://img.shields.io/badge/all%20three%20modes-no%20account%20needed-brightgreen)

Scrapes three things from [binance.com](https://www.binance.com) that its
pages show every visitor, into JSON and CSV:

| `--mode` | what | one row per | per page |
|---|---|---|---|
| `p2p` (default) | the **P2P order book** for an asset/fiat pair: price, limits, payment methods, the advertiser's 30-day completion and feedback | advert | 20 adverts |
| `copytrading` | **Futures copy-trading lead portfolios**: ROI, PnL, max drawdown, win rate, AUM, copier PnL, copiers and seats, badge | portfolio | 30 portfolios |
| `announcements` | an **announcement catalogue**: new listings, delistings, news, maintenance, API updates, airdrops | article | 50 announcements |

Every run writes a `<out>.meta.json` beside the output with the site's own
total, so a file can say "90 of 8,920" rather than only "90".

Binance's official API is for market data and your own account. Its
announcement feed is an [API-key-authenticated WebSocket](https://developers.binance.com/docs/cms/general-info).
This repo reads the public lists the site's own pages are built from.

---

## Start with the part most scrapers bury

**You need no key, no proxy and no account for any of the three modes.**

Measured 2026-09-24 from a datacentre VPS (netcup, Nuremberg):

| what was asked | answer |
|---|---|
| any HTML page on binance.com, plain curl | HTTP 202, empty body, `x-amzn-waf-action: challenge` (AWS WAF) |
| an announcement page in real Chromium, headless and headful | "Human Verification": AWS WAF's CAPTCHA |
| the three JSON endpoints the site's own front end calls, plain curl | **HTTP 200 and complete JSON** |

So the pages are gated and the data is not. Each engine lands a browser on
one of those endpoints and issues every page as a same-origin `fetch()`, so
no page is ever rendered. A full USDT/EUR buy-side order book, 10 pages,
came back as **196 of 196 adverts** in 13 seconds with nothing configured.

What the paid products buy here is insurance and scale, and the section
below says exactly which one does what.

---

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
./venv/bin/playwright install chromium
```

Install **one** engine per virtualenv: the three libraries pin versions of
their dependencies that cannot all be satisfied at once.

## Run

```bash
# P2P: adverts you could BUY USDT from, paying EUR, every page
./venv/bin/python playwright_scraper.py --asset USDT --fiat EUR --side buy --pages 50

# ...only those taking SEPA Instant (the site's identifier, checked before the search)
./venv/bin/python playwright_scraper.py --fiat EUR --pay-type SEPAinstant

# copy-trading: the top 150 portfolios by 7-day PnL
./venv/bin/python playwright_scraper.py --mode copytrading --time-range 7D --sort-by pnl --pages 5

# announcements: the delisting catalogue
./venv/bin/python playwright_scraper.py --mode announcements --category delisting --pages 2

# or read the query from a page's address
./venv/bin/python playwright_scraper.py --url "https://p2p.binance.com/en/trade/sell/BTC?fiat=TRY"
```

`--pages` is planned against the total the site states on page 1, so asking
for more pages than exist fetches all of them and stops.
[sample_output.json](sample_output.json),
[sample_output_copytrading.json](sample_output_copytrading.json) and
[sample_output_announcements.json](sample_output_announcements.json) are five
rows of each mode, cut from real runs. P2P rows have 29 columns,
copy-trading rows 26 columns and announcement rows 12 columns.

---

## Five things about Binance that will look like bugs

### 1. A buy query returns adverts marked "sell"

An advert carries the **maker's** side. Asking for adverts you can buy from
returns adverts whose own `tradeType` is `SELL`: 20 of 20 on the captured
page, and 20 of 20 the other way round on a sell query. Every row keeps
both: `side` is what you asked for, `advertiser_side` is what the advert
says.

### 2. The API accepts wrong values and answers with something plausible

Measured on 2026-09-24:

| sent | answer |
|---|---|
| copy-trading `dataType` = a made-up key | HTTP 200, a full list, under some other ordering |
| copy-trading `pageSize` = 50 or 100 | HTTP 200 with **30** rows: silently capped |
| P2P `payTypes` = an identifier with a typo | HTTP 200, `total: 0`, on a market full of adverts |

Each of those turns a typo into a run that looks healthy, so every
parameter is allowlisted before anything is sent, and `--pay-type` is
checked against the site's own list of payment methods for the fiat.
`--pay-type "SEPA Instant"` (the name the page shows) is refused with
"did you mean SEPAinstant?".

Win rate is **not** offered as a `--sort-by`: the endpoint gave a nonsense
key the same answer as `WIN_RATE`, so there is no evidence it sorts by it.

### 3. `--sort-by` decides which portfolios are in the file

Copy-trading lists about 8,900 portfolios. A 5-page run holds the first 150
**by the ordering you chose**, so the top 150 by ROI and the top 150 by AUM
are different samples, not the same one reordered. The ordering is a
column (`sort`), and `diff_runs.py` refuses to compare runs that differ in
it. `--sort-by sharpe` also filters: it returned 5,568 portfolios against
8,920 under every other key, because a portfolio with no Sharpe ratio is
left out. And `--sort-by mdd` with the default `desc` puts **zero** drawdown
first; that ordering is the site's.

### 4. The listings move while you read them

P2P's total went from 186 to 187 between two requests a minute apart. A
multi-page run of a live listing can see one row twice (the dedupe drops it
and the log says so) or miss one that moved up across a page boundary after
its page was fetched. No scraper can see the second.

### 5. A refused parameter is not a block

The announcements endpoint takes a page size from a fixed set, and answers
anything else (12, 25, 30, 100) with **HTTP 400 and an empty body**. Classify
that as a block and you go shopping for a proxy to fix a typo. Here it is
`rejected`: the run stops at once with the site's own complaint, nothing is
retried, and the exit code is 5 ("the data never arrived"), not 3.

---

## Engines

| | |
|---|---|
| `playwright_scraper.py` | **Primary.** Authenticates a proxy and a remote CDP endpoint. |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained; here for parity. Authenticates a proxy and a CDP endpoint. `--chromium-path` points it at another browser if its own will not start. |
| `selenium_scraper.py` | Drives the Chrome you already have. **Cannot authenticate a proxy or a remote CDP endpoint** (`debuggerAddress` is a bare `host:port`), so it refuses a credentialled `--cdp-endpoint` with exit 2. None of the modes needs either. |
| `scraper_api_client.py` | No local browser: the 2Captcha Scraper API fetches the page. Reads `--mode announcements` only, the one endpoint with a URL. The P2P and copy-trading endpoints answer POST, and **this repo does not implement a POST through the Scraper API**. Measured: 2 pages, 100 rows, $0.0005 a page. |

The fetch loop itself (landing, the WAF, retries, throttling, rotation,
parsing) is one implementation in `page_flow.py` that all three browser
engines drive, so they cannot disagree about a page. Each was run live on
2026-09-24 through the same seven scenarios (P2P buy and sell, copy-trading,
announcements, `--concurrency`, a refused `--pay-type`, `--url`) with the
same results.

---

## What the 2Captcha products buy, and when

One key, four separately-billed products ([2captcha.com](https://2captcha.com)):

* **Captcha solving**: AWS WAF's CAPTCHA, with the AmazonTask and
  AmazonTaskProxyless task types
  ([docs](https://2captcha.com/api-docs/amazon-aws-waf-captcha)). None of
  the data endpoints showed that CAPTCHA to any engine, so a normal run buys
  nothing. The path exists for the day the WAF moves in front of them. It
  was run live on a page that IS gated, a binance.com announcement page:

  | | |
  |---|---|
  | one solve | 23-40 s, $0.00145, page served |
  | the same session afterwards | three more gated pages, no further solve |
  | control: the same page, no solve | 0 of 5 cleared by themselves in 25 s |

  The part that matters if you port this: the value that clears the WAF is
  the solution's `existing_token`, set as the `aws-waf-token` cookie on the
  **registrable** domain (`.binance.com`). A `captcha_voucher` set as the
  cookie on the page's own host left the page on "Human Verification".
  Selenium's Chrome was not shown the CAPTCHA at all on those pages (3 of 3),
  so its copy of the solve path has not been exercised live.
* **Proxies** (`--proxy`, `--proxy-file`): volume from more than one
  address, and an exit in a country Binance serves. Binance's terms exclude
  some jurisdictions, the United States among them.
* **The Scraping Browser API** (`--cdp-endpoint`): a remote browser you do
  not run, with a chosen exit country. One live connection per `pid`, so
  `--concurrency` is refused with it. Run live on 2026-09-24 through a
  `country-de` profile: Playwright and pyppeteer, all three modes, 8 of 8
  runs complete, back to back. Two details measured on the way: a profile
  stays locked for 1.6-1.9 s after a clean disconnect, so the engines retry
  a `profile_locked` connection rather than failing on it; and an expired
  profile answers HTTP 401, which the engines report as exit 5 naming the
  expiry. Selenium refuses a credentialled endpoint with exit 2.
* **Fingerprints** (`--fingerprint`): a consistent device identity for a
  local browser. Ignored with `--cdp-endpoint`, which brings its own.

Nothing here integrates a competitor.

---

## Exit codes

| | |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage, including a `--pay-type` the site does not offer |
| 3 | blocked: AWS WAF, a 403 or a 451, distinct from an empty listing |
| 4 | zero rows: the listing has nothing in it |
| 5 | the data never arrived: a timeout, a dead proxy, a refused parameter, a remote API error |
| 6 | partial: some pages came back and some did not |

**A run that finds nothing writes nothing**, so a failure never replaces last
night's good output with `[]`. `--allow-empty` is the opt-out.

`diff_runs.py --old a.json --new b.json` compares two runs of the same mode
and query by `sku`: new and vanished adverts, portfolios or articles, and
every tracked column that changed.

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

The fixtures are real API responses, trimmed and scrubbed by
`make_fixtures.py`, which proves each one parses identically to its
original. The suite also drives the shared fetch loop end to end with a fake
browser: a full listing, a refused parameter, a 403, an empty listing, a
throttle, a WAF challenge mid-run and a bad `--pay-type`.

The [canary](.github/workflows/canary.yml) runs a real 3-page scrape of each
mode daily **with no secrets**, which is what keeps "no account needed"
honest. Its first dispatch (2026-09-24, a GitHub-hosted runner, no proxy)
came back complete in all three modes: 60 of 238 adverts, 90 of 8,917
portfolios, 150 of 2,269 announcements.

---

## Legal

This reads **public data**: the P2P advert list, the public copy-trading
leaderboard and the announcement catalogues, as the site's own pages fetch
them for an anonymous visitor. It places no order, opens no trade, copies no
portfolio and reads nothing behind a login.

Rate limits, terms of service and the legality of scraping in your
jurisdiction are your responsibility as the operator. `--delay` defaults to
1 second between pages.

MIT licensed. Not affiliated with or endorsed by Binance.
