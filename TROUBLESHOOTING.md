# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

## Exit 2 — bad usage

* **"--pay-type X is not a payment method P2P offers for this fiat"** — the
  search would have answered with an empty result, so the run was stopped
  before it started. Use the identifier from the suggestion
  (`SEPAinstant`, not "SEPA Instant"). A method can also genuinely not
  exist for a fiat: Wise is not offered for TRY.
* **"--url already carries the query"** — pass a URL or the query flags, not
  both. Merging them could scrape something neither named.

## Exit 3 — blocked

The log names what refused the run:

* **`aws-waf`** — AWS WAF challenged the session. The data endpoints were
  not gated when this repo was written, so this is news: please report it
  with the exit you used. A residential `--proxy`, or `TWOCAPTCHA_KEY` in
  `.env` so the WAF's CAPTCHA can be solved (AmazonTask), are the two
  answers.
* **`http-403`** — the site refused the address. Binance's API documentation
  uses 403 for a WAF rule, including a rate-limit violation. Slow down
  (`--delay`), or use a different exit.
* **`geo-451`** — the exit's country is refused. Binance's terms exclude some
  jurisdictions, the United States among them.

A `<out>_page<N>_debug.html` beside the output holds what came back.

## Exit 4 — zero rows

The listing genuinely has nothing in it, for example a P2P market with no
adverts for that payment method or amount. Nothing is written, so an
earlier good file is left alone; `--allow-empty` writes the empty file.

## Exit 5 — the data never arrived

* **"The endpoint refused this request (code ...)"** — the site rejected the
  parameters. The same request would be rejected again, so it is not
  retried. If the query looks right, the API changed: open a "Site changed"
  issue with `--dump-html` output.
* **"Gave up on page N"** — a timeout or a dead proxy. The log names which.
* **"Still rate-limited"** — the site kept answering 429/418. Raise
  `--delay`, lower `--concurrency`, or spread the run over `--proxy-file`.

## Exit 6 — partial

Some pages came back and a later one did not. The output holds what was
gathered and the sidecar lists `pages_failed` by number.

## A run looks fine but a column is wrong

Re-run with `--dump-html response.json`: it writes the exact JSON the parser
was given, on success too, so a parsing bug can be told apart from a change
in what the site sends.

## Duplicates dropped, or a row missing between two runs

The listings are live. A row that moves across a page boundary while a run
reads the pages is fetched twice (the dedupe drops it and the log says so),
or not at all. That is the site moving, not the scraper.
