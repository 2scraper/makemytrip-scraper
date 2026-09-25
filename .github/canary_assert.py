#!/usr/bin/env python3
"""The canary's assertions on a finished run: `canary_assert.py PREFIX FLOOR`.

A separate script rather than a heredoc in canary.yml, and it reads only the
run's own output files: a workflow that imports the repo's modules inline is
red on the first push and invisible to every local run (CLAUDE.md §26).

The thresholds are measured: a three-page Goa run through the Scraping
Browser returned 90 rows, 90 priced, all in INR, all RECOMMENDED_HOTELS, on
2026-09-24. The floor leaves room for a thin listing.
"""
import json
import sys

prefix, floor = sys.argv[1], int(sys.argv[2])
meta = json.load(open(prefix + ".meta.json"))
rows = json.load(open(prefix + ".json"))
problems = []
if meta["status"] != "complete":
    problems.append("status %r (%s)" % (meta["status"], meta["stop_reason"]))
if meta["pages_completed"] != 3 and meta["stop_reason"] != "end_of_listing":
    problems.append("pages_completed %r, not 3" % meta["pages_completed"])
if len(rows) < floor:
    problems.append("%d rows, floor %d" % (len(rows), floor))
pairs = [(r["page"], r["position"]) for r in rows]
if len(set(pairs)) != len(pairs):
    problems.append("page+position is not unique")
if len({r["sku"] for r in rows}) != len(rows):
    problems.append("a sku appears twice")
priced = sum(1 for r in rows if isinstance(r["price"], float))
if priced < 0.95 * len(rows):
    # The price vanishing from EVERY row is what losing `expData` from the
    # request looks like (product_parser.EXP_DATA).
    problems.append("only %d of %d rows priced" % (priced, len(rows)))
if {r["currency"] for r in rows} != {"INR"}:
    problems.append("currencies %r, not INR" % {r["currency"] for r in rows})
if any(r["original_price"] is not None and r["original_price"] <= r["price"] for r in rows):
    problems.append("an original_price at or below its price")
if any(r["rating"] == 0 or r["star_rating"] == 0 for r in rows):
    problems.append("a zero rating or zero stars (should be null)")
if any(r["section"] != "RECOMMENDED_HOTELS" for r in rows[:30]):
    problems.append("substitutes on page 1 of Goa")
if problems:
    print("canary FAILED: " + "; ".join(problems))
    sys.exit(1)
print("canary OK: %d rows, %d priced, %s" % (len(rows), priced, meta["status"]))
