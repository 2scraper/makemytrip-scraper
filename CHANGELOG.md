# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

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
