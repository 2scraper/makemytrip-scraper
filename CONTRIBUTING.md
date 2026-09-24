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

Binance changing its API is the normal way this stops working, and it has
its own issue template. The parser reads no HTML at all: every row comes out
of one of three JSON endpoints the site's own front end calls
(`product_parser.py` names them). So there are only four things that can
break, and each is loud or guarded:

1. **The envelope.** Every endpoint answers `{"code": "000000", "data": …}`.
   A non-success code is classified `rejected` and the run stops naming the
   site's own complaint. It is not retried and not counted as blocked.
2. **A parameter the endpoint stops accepting.** Same path: `rejected`, with
   the site's message. The first place to look is the allowlists at the top
   of `product_parser.py`, which exist because the API accepts several wrong
   values SILENTLY (see "Pull requests" below).
3. **A record's own field names** (`adv.price`, `advertiser.nickName`,
   `roi`, `leadPortfolioId`, `releaseDate`, ...). This is the one that can be
   QUIET: the row still writes, with that column null. `page_flow.CORE_FIELDS`
   is the guard, a coverage floor of 99% on the columns every captured record
   carried.
4. **The endpoints going behind AWS WAF.** Every HTML page on the site
   already is. If the endpoints follow, the README's central claim (no key,
   no proxy) stops being true, and the canary will say so, because it runs
   with no secrets from a GitHub runner.

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

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   It runs daily with no secrets at all and is expected to be green, because
   no mode needs a credential and a green badge there is exactly the claim
   the README makes. Its first dispatch (2026-09-24) was served in all
   three modes from a GitHub-hosted runner with no proxy. If a later run
   is refused for the runner's address, the canary's `BINANCE_PROXY`
   secret (an exit elsewhere) is the fix, with no workflow edit.
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

- **Every query parameter is allowlisted, because the API does not validate.**
  An unknown copy-trading `dataType` returns a full list under some OTHER
  ordering; `pageSize` above 30 is silently capped at 30; an unknown P2P
  `payTypes` entry returns an empty result for a market full of adverts.
  Each of those turns a typo into a run that looks healthy. `--pay-type` is
  therefore checked against the site's own list for the fiat before the
  search runs.
- **The P2P side is inverted in the data.** A `buy` query returns adverts
  whose `tradeType` is `SELL`, because an advert carries the maker's side.
  Rows keep both, as `side` (what was asked) and `advertiser_side`.
- **A refused parameter is `rejected`, not blocked.** The announcements
  endpoint answers a page size outside {1, 2, 5, 10, 15, 20, 50} with HTTP 400
  and an EMPTY body. Classified as a block, that would send a reader to buy a
  proxy for a typo.
- **Pages are planned from page 1's total**, and a page past the end is an
  answer, not an error: all three endpoints return an empty list there. P2P
  also reports `total: 0` on that page, which is why only page 1's total is
  ever read.
- **The listings are live**, so a multi-page run can see a row twice. The
  dedupe drops it and the log says so. A non-zero count there is the site
  moving, not a bug.
- **AWS WAF: the token that clears the CAPTCHA is `existing_token` on the
  registrable domain.** A `captcha_voucher` set as the cookie on the page's
  own host left the page on "Human Verification". `captcha_solver` and
  `page_flow.cookie_domain` pin the version that was measured to work.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` remote API error, `6`
  partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier and
the CLI contract against real, trimmed responses. If yours genuinely needs
binance.com, say in the PR what you ran (engine, mode, query), from which
exit, and what you got, including the sidecar's `total_results`.

Two things about running this live that are specific to Binance:

* **No mode needs an exit, a key or an account.** The endpoints answered a
  datacentre VPS normally, so "it worked from my laptop" is reproducible
  here in a way it is not on most sibling repos.
* **Binance's terms exclude some jurisdictions, the United States among
  them** (binance.us is a separate exchange). What an address there is
  answered with has not been measured by this repo. If a run from one is
  refused, that is the terms, not a bug.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own, and only running it proves it.

## Scope

This repo reads **public data** on binance.com: the P2P advert list, the
public copy-trading leaderboard and the announcement catalogues, exactly as
the site's own front end fetches them for an anonymous visitor.

Out of scope: anything behind a login, anything that places an order,
opens a P2P trade, copies a portfolio or submits any other form, and
anything that defeats a protection rather than passing it the way an
ordinary browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
