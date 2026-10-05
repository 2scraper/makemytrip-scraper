#!/usr/bin/env python3
"""Print the versions a canary run actually had: `env_report.py [--chromium]`.

Why it exists: when the canary goes red, the first question is whether the
SITE moved or the runner did (Python, the TLS library, the driver, the
browser). This answers it from the log of the run itself. It prints versions
and the kind of endpoint, never an endpoint: a Scraping Browser URL carries
its credentials in the login, and a log is a file people share.

`--chromium` also launches the local Chromium to read its version, for the
job that drives one; the Scraping Browser job has no local browser to ask.
"""
import os
import platform
import ssl
import sys


def version_of(dist):
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return "not installed"


print("python      %s" % platform.python_version())
print("platform    %s" % platform.platform())
print("tls library %s" % ssl.OPENSSL_VERSION)
for dist in ("requests", "urllib3", "beautifulsoup4", "playwright"):
    print("%-11s %s" % (dist, version_of(dist)))
print("endpoint    %s" % ("remote CDP (Scraping Browser)"
                          if os.environ.get("MAKEMYTRIP_CDP_ENDPOINT")
                          else "local browser, no endpoint"))

if "--chromium" in sys.argv:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            print("chromium    %s" % browser.version)
            browser.close()
    except Exception as exc:
        # A report must not fail the job it is describing.
        print("chromium    could not be launched: %s" % type(exc).__name__)
