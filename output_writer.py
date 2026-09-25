"""
output_writer.py
-----------------
The row model + JSON/CSV writers shared by all three engines.

One mode, one row class
-----------------------
    --mode hotels    Hotel    one property on a hotel listing

The family prefix is byte-identical and in order: `source`, `scraped_at`,
`url`, `sku`, `title`, then `price`/`original_price`/`currency`, so a
consumer reading several repos in this family reads the same first columns.
The run-describing tail — `page`, `position`, `mode`, `data_source` — is
shared too. Everything site-specific sits between them.

`Product` is kept as an alias of `Hotel`: the family's tooling imports the
family name.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# Every row is read from makemytrip.com's own listing API. The value is a
# constant so it cannot vary with which host a run happened to land on.
SOURCE_DEFAULT = "makemytrip.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Hotel:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The property's own detail page for THIS stay (dates and guests are in
    # the address), as the listing states it in `detailDeeplinkUrl`.
    url: str = ""
    # The site's hotel id: 15-18 digits, the same id its detail page and its
    # own `hotelId=` parameter use.
    sku: Optional[str] = None
    title: Optional[str] = None
    # Per NIGHT, before taxes, after the site's own discount: the figure the
    # listing prints. Measured 2026-09-24: a 3-night search returned the same
    # per-night figures as a 1-night one, and every record says
    # `priceDisplayMsg: "Per Night"` (kept as `price_basis`).
    price: Optional[float] = None
    # The struck-through figure, when the site shows one. None when it equals
    # `price`: that is no discount, not a discount of zero.
    original_price: Optional[float] = None
    # ISO 4217, from the response's own `currency`. It follows the site's
    # REGION, not the property: Dubai hotels are priced in INR on
    # makemytrip.com, with the city tax stated separately in AED.
    currency: Optional[str] = None
    discount_pct: Optional[float] = None
    # Per night, with taxes: what "+ ₹145 taxes & fees" adds up to on a tile.
    price_with_tax: Optional[float] = None
    # Per night, with taxes AND the fees collected at the property (a Dubai
    # city tax, for instance). Equal to `price_with_tax` where there are none.
    price_with_fees: Optional[float] = None
    price_basis: Optional[str] = None
    coupon_code: Optional[str] = None
    coupon_discount: Optional[float] = None
    property_type: Optional[str] = None
    # 1-5. None for a property the site gives no stars (hostels, homestays
    # and villas carry `starRating: 0`, which is "unrated", not nought).
    star_rating: Optional[int] = None
    # The guest rating out of 5. None, together with its counts, when no one
    # has rated the property: a 0 there would drag every average (§21).
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    review_count: Optional[int] = None
    rating_text: Optional[str] = None
    # Whose reviews the rating summarises, in the site's own code: MMT,
    # MMT_EXP, MMT_BKG, TA ... Two ratings from different sources are not
    # the same scale of evidence.
    review_source: Optional[str] = None
    locality: Optional[str] = None
    city: Optional[str] = None
    city_code: Optional[str] = None
    # The site's country code, NOT ISO 3166: the United Arab Emirates is
    # "UNI" there (measured on Dubai, 2026-09-24).
    country_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image: Optional[str] = None
    categories: Optional[List[str]] = None
    sold_out: Optional[bool] = None
    sponsored: Optional[bool] = None
    # Which block of the listing the property came from, by the site's own
    # name: RECOMMENDED_HOTELS for the city asked for, NEARBY_HOTELS when the
    # site ran out of it (or never had it) and substituted properties from
    # somewhere else. The column is what tells those apart.
    section: Optional[str] = None
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    adults: Optional[int] = None
    rooms: Optional[int] = None
    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # The ordering the listing was read under. It decides WHICH properties a
    # capped run holds, so two runs under different sorts are two samples.
    sort: Optional[str] = None
    data_source: Optional[str] = None


# The family name, kept for tooling that imports it (see the docstring).
Product = Hotel

# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"hotels": Hotel}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py.
UNIQUE_BY_SKU_MODES = ("hotels",)


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.

    The listing is paged by a CURSOR the server hands back (the last hotel
    id and a window marker), and whether pages overlap depends on the
    ordering. Measured 2026-09-24: 0 duplicates across 90 Goa properties on
    three pages of the default ordering, and 4 of 30 on page 2 of Mumbai
    under `price-asc`, where properties at the same price straddle the
    cursor. The duplicates are dropped here and the count is logged, so a
    `price-asc` page can hold fewer than 30 new rows.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Hotel) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked before any data arrived: Akamai's "Access
# Denied" (HTTP 403), or the connection reset Akamai answers a datacentre
# address with. Distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# listing genuinely has nothing in it" from "something stood between us and
# the listing".
#
# An empty listing is NOT this code. A city the site has no properties for
# answers HTTP 200 with its own "No Hotels Found" envelope, and that is
# EXIT_NO_PRODUCTS: the request was served exactly as asked.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded so diff_runs.py can refuse a pair
    whose modes or sources differ, as it does across the family.

    `extra` carries facts about the run that are not about any single row:
    the query that was sent (city, dates, guests, ordering) and whether the
    site said the listing had ENDED (`end_of_listing`). The listing states
    no total, so a run that stopped at --pages is complete as a REQUEST and
    of unknown size as a LISTING, and only the sidecar can say which.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these are hotels, and kept that way
        # deliberately: every repo in this family
        # writes this key, and a consumer reading several of them reads one
        # sidecar shape. The row TYPE is `mode`, right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Hotel) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On this site there is a third and stronger signal: the listing API says
# so itself. Every response carries `noMoreHotels`, and a run that reaches a
# page where it is true ends "end_of_listing". That is complete, because
# there was nothing more to get. The site states no total up front, so a
# run is never PLANNED against one; see page_flow.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Hotel)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
