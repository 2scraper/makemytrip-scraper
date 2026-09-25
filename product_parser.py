"""
product_parser.py
-----------------
Everything this repo knows about makemytrip.com lives here (CLAUDE.md §1).

One mode, the front end's own listing API
-----------------------------------------
    --mode hotels   POST mapi.makemytrip.com/clientbackend/cg/search-hotels/DESKTOP/2
                    one row per property on a city's hotel listing

The listing page itself carries no products worth parsing: it server-renders
five tiles and then asks this endpoint for the rest, ten at a time, as the
visitor scrolls. The endpoint's JSON is richer than the tile (price before
and after taxes, the fees paid at the property, the rating's source,
coordinates), so it is the only source read. There is no JSON-LD product
data on the page to cross-check it against: the listing carries two
`application/ld+json` blocks and neither is a list of hotels.

Why a browser at all
--------------------
Measured 2026-09-24 from a datacentre VPS (netcup, AS197540):

    www.makemytrip.com, plain curl            HTTP 403, Akamai "Access Denied"
    the same with a Chrome User-Agent         the connection is reset, or hangs
    headless and headful Chromium             net::ERR_HTTP2_PROTOCOL_ERROR
    mapi.makemytrip.com (this endpoint)       HTTP 403, Akamai "Access Denied"
    the Scraping Browser API, country-in      HTTP 200, the full listing

So both the pages and the API are behind Akamai Bot Manager, and what
changes the answer is the ADDRESS, not the client: real Chromium is refused
as hard as curl from a datacentre, and served from an Indian exit. The
engines land a real browser on www.makemytrip.com (for the origin and the
cookies Akamai's sensor sets) and issue each page as a `fetch()` from there,
with the headers the site's own front end sends. Without those headers the
endpoint answers without CORS permission and the browser reports "Failed to
fetch" (measured: the same request with only `content-type` failed, with
the front end's header set it returned 170 KB).

No captcha was met anywhere on this site
----------------------------------------
Every served page was counted for captcha markers after removing what the
Scraping Browser's auto-solve extension injects (§24): 0 reCAPTCHA, 0
hCaptcha, 0 Turnstile, 0 sitekeys, 0 GeeTest. Akamai refuses by dropping the
connection or by its 400-byte "Access Denied" page, and neither carries a
widget. So the solver path is the family's broad DETECTION, kept as
insurance, and has never had anything to solve here.

The API answers some mistakes with plausible data
-------------------------------------------------
Measured 2026-09-24, each on the live endpoint:

    cityCode=CTLEH (not Leh's code)   HTTP 200, 7 hotels in PATRAN, under a
                                      section named NEARBY_HOTELS and the
                                      heading "No more properties in Lehra"
    cityCode=CTXXXX                   HTTP 200, error 400814 "No Hotels Found"
    sortCriteria.field=starRating     HTTP 200, error 400108 "field name is
                                      not supported"
    limit=100                         HTTP 200, 63 hotels: capped, no error

The first is the dangerous one: a typo in a city code produces a healthy-
looking file about another town. So every row carries the SECTION it came
from, and page_flow warns when a page is nothing but substitutes. The city
name is better still: `--city Goa` is resolved through the site's own
autosuggest, which is what the search box does.
"""

import html as html_lib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from output_writer import Hotel, SOURCE_DEFAULT  # noqa: F401

BASE = "https://www.makemytrip.com"
API_BASE = "https://mapi.makemytrip.com"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

SEARCH_PATH = "/clientbackend/cg/search-hotels/DESKTOP/2"
# The search box's own lookup: a place name -> the site's location code and
# ITS country code. Answers GET, ungated from the landing's origin.
SUGGEST_PATH = "/autosuggest/v5/search"

# Where a browser engine lands before its first request. It needs to be on
# www.makemytrip.com, so the page has the origin the endpoint grants CORS
# to, and it is where Akamai's sensor script runs and sets its cookies. The
# hotels home page does both and is a third the size of a listing page
# (403 KB against 1.2 MB, 2026-09-24).
ORIGIN_URL = BASE + "/hotels/"

# ---------------------------------------------------------------------------
# Page size and ceilings, measured
# ---------------------------------------------------------------------------

# The site's own front end asks for 10. 30 and 50 were each returned in
# full; 100 returned 63, so the server caps somewhere between. 30 is well
# inside what it honours and a third of the requests of 10.
ROWS_PER_PAGE = 30

# A ceiling on --pages, so a typo cannot start a ten-thousand-request run.
# The listing states no total, so there is nothing better to plan against:
# Goa alone kept answering past 90 properties with more to come.
MAX_PAGES = 200

# The longest stay the search form offers.
MAX_NIGHTS = 30
MAX_ROOMS = 8
MAX_ADULTS_PER_ROOM = 12

# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

# --sort -> the API's `sortCriteria`. Only keys PROVEN to change the order
# are listed; each gave a different first eight on Goa, 2026-09-24:
#
#     popular      (null)            the site's default: `S_hsq910_Q` asc
#     price-asc    price asc         609, 616, 655, 659 ... INR
#     price-desc   price desc        241934, 209154, 207000 ... INR
#     rating       reviewRating desc 4.9, 5.0, 4.9, 4.9 ...
#
# `starRating` and `userRating` were refused with error 400108, so neither
# is offered. None of the 34 properties on four default-ordered pages was
# marked `sponsored`, so the default ordering is not a paid placement as far
# as this sample shows; the column is kept so a later run can show
# otherwise.
SORTS: Dict[str, Optional[Dict[str, str]]] = {
    "popular": None,
    "price-asc": {"field": "price", "order": "asc"},
    "price-desc": {"field": "price", "order": "desc"},
    "rating": {"field": "reviewRating", "order": "desc"},
}
DEFAULT_SORT = "popular"

# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

# The headers the site's own front end sends with every listing request,
# captured 2026-09-24. The endpoint grants CORS only to requests carrying
# them. `vid`/`visitor-id` are a random id per run, like a first visit;
# `usr-mcid` (an Adobe visitor id) was left out and made no difference.
API_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "currency": "INR",
    "entity-name": "india",
    "language": "eng",
    "os": "desktop",
    "region": "IN",
    "server": "b2c",
    "tid": "avc",
    "user-country": "IN",
    "user-currency": "INR",
}


# The front end's experiment flags, sent verbatim as the site sends them
# (captured 2026-09-24). NOT optional: without this string the endpoint
# returns every property with no `priceDetail` at all. Measured on one Goa
# request, same session: 30 of 30 priced with it, 0 of 30 without, and
# neither the full `featureFlags` nor the full `requestDetails` brought the
# prices back. A first live run without it produced 81 rows with every
# price null, which the core-field floor in page_flow reported at once.
EXP_DATA = '{APE:10,PAH:5,PAH5:T,WPAH:F,BNPL:T,MRS:T,PDO:PN,MCUR:T,ADDON:T,CHPC:T,AARI:T,NLP:Y,RCPN:T,PLRS:T,MMRVER:V3,BLACK:T,IAO:F,BNPL0:T,EMIDT:1,HAFC:T,CRI:T,ALC:T,LSTNRBY:T,PLV2:T,HIS:DEFAULT,HFC:T,VIDEO:0,MLOS:T,CV2:T,SOU:T,APT:T,AIP:T,PERNEW:T,RTBC:T,PCCE:T,FLTRPRCBKT:T,UGCV2:T,CRF:T,GALLERYV2:T}'


@dataclass
class ApiRequest:
    """One call to the site's API: what an engine sends.

    `body` is None for a GET. `label` is what a log line uses, because a
    POST has no address that tells two pages apart.
    """
    method: str
    path: str
    page: int
    params: Dict[str, Any] = field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        q = ("?" + urlencode(self.params)) if self.params else ""
        return API_BASE + self.path + q

    @property
    def body_json(self) -> Optional[str]:
        return None if self.body is None else json.dumps(self.body)

    @property
    def label(self) -> str:
        return "%s %s page=%d" % (self.method, self.path.rsplit("/", 3)[-3],
                                  self.page)


@dataclass
class Room:
    adults: int = 2
    child_ages: Tuple[int, ...] = ()


@dataclass
class Query:
    """What a run asks for, independent of the page.

    `city` is what the user typed (a name, or a location code); `city_code`
    and `country_code` are what the API needs. A name is resolved through
    the site's autosuggest before page 1 (page_flow.resolve_city), which is
    why both halves exist.
    """
    mode: str = "hotels"
    city: Optional[str] = None
    city_code: Optional[str] = None
    city_name: Optional[str] = None
    country_code: Optional[str] = None
    locus_type: str = "city"
    checkin: Optional[date] = None
    checkout: Optional[date] = None
    rooms: Tuple[Room, ...] = (Room(),)
    sort: str = DEFAULT_SORT
    # One id per run, as a first visit would have. Not a secret: the site
    # issues it to every anonymous visitor.
    visitor_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def adults(self) -> int:
        return sum(r.adults for r in self.rooms)

    @property
    def nights(self) -> Optional[int]:
        if self.checkin and self.checkout:
            return (self.checkout - self.checkin).days
        return None

    def validate(self, today: Optional[date] = None) -> Optional[str]:
        """None when the query can be sent, else the reason it cannot."""
        today = today or date.today()
        if self.mode != "hotels":
            return "unknown mode %r" % self.mode
        if not (self.city or self.city_code):
            return ("--city is required: a place name (Goa, Mumbai, Dubai) or "
                    "the site's location code (CTGOI).")
        if self.city_code and not is_location_code(self.city_code):
            return "%r is not a location code (CTGOI, CTBOM ...)" % self.city_code
        if self.checkin is None or self.checkout is None:
            return "--checkin and --checkout are both required (YYYY-MM-DD)"
        if self.checkin < today:
            return ("--checkin %s is in the past; the site prices stays, and a "
                    "past date has no price." % self.checkin.isoformat())
        n = self.nights
        if n is None or n < 1:
            return "--checkout must be after --checkin"
        if n > MAX_NIGHTS:
            return "at most %d nights per search, as on the site" % MAX_NIGHTS
        if not self.rooms or len(self.rooms) > MAX_ROOMS:
            return "--rooms must be 1-%d" % MAX_ROOMS
        for r in self.rooms:
            if not 1 <= r.adults <= MAX_ADULTS_PER_ROOM:
                return "--adults must be 1-%d per room" % MAX_ADULTS_PER_ROOM
            if any(not 0 <= a <= 17 for a in r.child_ages):
                return "a child's age must be 0-17"
        if self.sort not in SORTS:
            return "--sort must be one of %s" % ", ".join(SORTS)
        return None


_LOCATION_CODE_RE = re.compile(r"^CT[A-Z0-9]{2,12}$")


def is_location_code(text: Optional[str]) -> bool:
    """Whether `text` is a city code in the site's own shape (CTGOI, CTDUB)."""
    return bool(text) and bool(_LOCATION_CODE_RE.match(text))


def parse_date(text: str) -> Optional[date]:
    """YYYY-MM-DD, or the site's own MMDDYYYY from a listing address."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%m%d%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def rooms_from_qualifier(text: str) -> Optional[Tuple[Room, ...]]:
    """The site's `roomStayQualifier` -> rooms, or None if malformed.

    The form is one group per room, each `{adults}e{children}e` followed by
    that many `{age}e`: "2e0e" is one room with two adults, "2e0e1e0e" two
    rooms, "2e2e5e8e" two adults with children aged 5 and 8.
    """
    parts = [p for p in (text or "").split("e")]
    if not parts or parts[-1] != "":
        return None
    nums = parts[:-1]
    if not nums or not all(p.isdigit() for p in nums):
        return None
    vals = [int(p) for p in nums]
    rooms: List[Room] = []
    i = 0
    while i < len(vals):
        if i + 1 >= len(vals):
            return None
        adults, kids = vals[i], vals[i + 1]
        ages = vals[i + 2:i + 2 + kids]
        if len(ages) != kids:
            return None
        rooms.append(Room(adults=adults, child_ages=tuple(ages)))
        i += 2 + kids
    return tuple(rooms) or None


def qualifier_for(rooms: Sequence[Room]) -> str:
    return "".join("%de%de%s" % (r.adults, len(r.child_ages),
                                 "".join("%de" % a for a in r.child_ages))
                   for r in rooms)


@dataclass
class Cursor:
    """Where the next page starts, as the server stated it.

    The listing is not addressed by page number. Each response names the
    last hotel it returned and a window marker, and the next request sends
    both back with a running count. So page N cannot be fetched before page
    N-1 (§7, §18), and --concurrency is refused.
    """
    last_hotel_id: str = ""
    window: str = ""
    shown: int = 0


def listing_url(query: Query) -> str:
    """The site's own address for this search: what a person would open."""
    params = {
        "checkin": query.checkin.strftime("%m%d%Y") if query.checkin else "",
        "checkout": query.checkout.strftime("%m%d%Y") if query.checkout else "",
        "city": query.city_code or "",
        "locusId": query.city_code or "",
        "locusType": query.locus_type,
        "country": query.country_code or "",
        "roomStayQualifier": qualifier_for(query.rooms),
    }
    return BASE + "/hotels/hotel-listing/?" + urlencode(params)


def request_for(query: Query, page: int, cursor: Optional[Cursor] = None) -> ApiRequest:
    """The API call that fetches page `page` (1-based) of `query`.

    Built from the body the site's own front end sends (captured
    2026-09-24), cut to the fields that shape the answer. Everything that
    identifies a person is absent or random: no user location, no Adobe id,
    a fresh request id per call.
    """
    cursor = cursor or Cursor()
    request_id = str(uuid.uuid4())
    body = {
        "deviceDetails": {"appVersion": "151.0.0.0", "deviceId": query.visitor_id,
                          "bookingDevice": "DESKTOP", "networkType": "WiFi",
                          "deviceType": "DESKTOP", "deviceName": None},
        "searchCriteria": {
            "checkIn": query.checkin.isoformat() if query.checkin else None,
            "checkOut": query.checkout.isoformat() if query.checkout else None,
            "limit": ROWS_PER_PAGE,
            "roomStayCandidates": [{"adultCount": r.adults, "rooms": 1,
                                    "childAges": list(r.child_ages)}
                                   for r in query.rooms],
            "countryCode": query.country_code,
            "cityCode": query.city_code,
            "locationId": query.city_code,
            "locationType": query.locus_type,
            "currency": "INR",
            "preAppliedFilter": False,
            "userSearchType": query.locus_type,
            "lastHotelId": cursor.last_hotel_id,
            "lastHotelCategory": "",
            "personalizedSearch": True,
            "nearBySearch": False,
            "totalHotelsShown": cursor.shown,
            "personalCorpBooking": False,
            "rmDHS": False,
            "lastFetchedWindowInfo": cursor.window,
        },
        "requestDetails": {
            "visitorId": query.visitor_id, "visitNumber": 1,
            "trafficSource": {"flowType": "funnel"}, "funnelSource": "HOTELS",
            "idContext": "B2C", "pageContext": "LISTING", "channel": "B2Cweb",
            "requestId": request_id, "sessionId": query.visitor_id,
            "subPageContext": "", "loggedIn": False,
        },
        "featureFlags": {
            "soldOut": True, "staticData": True, "freeCancellation": True,
            "coupon": True, "checkAvailability": True,
            "reviewSummaryRequired": True, "persuasionsRequired": False,
            "shortlistingRequired": False, "similarHotel": False,
            "personalizedSearch": True,
        },
        "imageDetails": {"types": ["professional"],
                         "categories": [{"type": "H", "count": 1, "height": 162,
                                         "width": 243, "imageFormat": "webp"}]},
        "reviewDetails": {"otas": ["MMT", "TA", "MANUAL"],
                          "tagTypes": ["BASE", "WHAT_GUESTS_SAY"]},
        "filterCriteria": [],
        "sortCriteria": SORTS.get(query.sort),
        "expData": EXP_DATA,
    }
    params = {"cityCode": query.city_code, "requestId": request_id,
              "language": "eng", "region": "in", "currency": "INR",
              "idContext": "B2C", "countryCode": query.country_code}
    headers = dict(API_HEADERS, vid=query.visitor_id,
                   **{"visitor-id": query.visitor_id})
    return ApiRequest("POST", SEARCH_PATH, page, params=params, body=body,
                      headers=headers)


def suggest_request(text: str) -> ApiRequest:
    params = {"q": text, "sf": "true", "sfn": "true", "isWebRequest": "true",
              "language": "eng", "region": "in", "currency": "INR",
              "idContext": "B2C", "countryCode": "IN"}
    return ApiRequest("GET", SUGGEST_PATH, 0, params=params)


def pick_suggestion(payload: Any, text: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """The city the autosuggest names for `text`: ({code, name, country}, None),
    or (None, reason).

    Only a CITY is taken, and only the site's first one, which is what the
    search box would select. A suggestion list with no city in it (a hotel
    name, a landmark) is refused with what it did offer, rather than
    guessed at.
    """
    items = _json_or_none(payload) if isinstance(payload, str) else payload
    if not isinstance(items, list):
        return None, "the site's autosuggest answered with something unreadable"
    offered = []
    for it in items:
        if not isinstance(it, dict):
            continue
        code, typ = _str(it.get("cityCode") or it.get("id")), _str(it.get("type"))
        name = _str(it.get("displayName") or it.get("name"))
        if typ == "city" and is_location_code(code):
            return {"code": code, "name": _str(it.get("cityName")) or name,
                    "country": _str(it.get("countryCode")) or "IN",
                    "display": name}, None
        if name:
            offered.append(name)
    if offered:
        return None, ("%r matched no city on makemytrip.com; the site "
                      "suggested %s. Pass a city, or its code." %
                      (text, "; ".join(offered[:4])))
    return None, "%r matched nothing on makemytrip.com" % text


# ---------------------------------------------------------------------------
# --url
# ---------------------------------------------------------------------------

SUPPORTED_HOSTS = ("www.makemytrip.com", "makemytrip.com")


def query_from_url(url: str) -> Tuple[Optional[Query], Optional[str]]:
    """Map a hotel listing address onto a Query: (query, None), or
    (None, reason) when the address is not one this repo reads.

        https://www.makemytrip.com/hotels/hotel-listing/?checkin=MMDDYYYY
            &checkout=MMDDYYYY&city=CTGOI&locusId=CTGOI&locusType=city
            &country=IN&roomStayQualifier=2e0e[&sort=...]
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host not in SUPPORTED_HOSTS:
        return None, ("%r is not a makemytrip.com address. makemytrip.global "
                      "and the regional sites are not supported."
                      % (parsed.hostname or url))
    if not re.match(r"^/hotels/hotel-listing/?$", parsed.path or ""):
        return None, ("%s is not a hotel listing. Supported: "
                      "/hotels/hotel-listing/?city=...&checkin=...&checkout=... "
                      "(flights are not implemented by this repo)." % url)
    qs = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    code = (qs.get("locusId") or qs.get("city") or "").upper()
    if not is_location_code(code):
        return None, "%s carries no city code (city=CT...)" % url
    rooms = rooms_from_qualifier(qs.get("roomStayQualifier", "2e0e"))
    if rooms is None:
        return None, ("roomStayQualifier=%r is not in the site's form (2e0e)"
                      % qs.get("roomStayQualifier"))
    checkin, checkout = parse_date(qs.get("checkin", "")), parse_date(qs.get("checkout", ""))
    if checkin is None or checkout is None:
        return None, "%s carries no checkin/checkout dates (MMDDYYYY)" % url
    locus = (qs.get("locusType") or "city").lower()
    if locus != "city":
        return None, ("locusType=%s: only city listings are implemented by "
                      "this repo." % locus)
    return Query(city=code, city_code=code,
                 country_code=(qs.get("country") or "IN").upper(),
                 checkin=checkin, checkout=checkout, rooms=rooms), None


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

# Akamai's refusal page, in the form it reaches a parser. It is 372-436
# bytes, titled "Access Denied", with a reference id and a link to
# errors.edgesuite.net. Raw bytes entity-escape the punctuation
# (`errors&#46;edgesuite&#46;net`) while a browser's DOM spells it plainly,
# so markers are matched after html.unescape over a bounded prefix (§20).
AKAMAI_DENY_MARKERS = ("errors.edgesuite.net", "You don't have permission to access")
# The reference id's own shape, whitespace-tolerant: Akamai has been seen to
# write it with two spaces on a sibling site (§24).
_AKAMAI_REF_RE = re.compile(r"Reference\s+#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+")

# A page the site actually SERVED is built from its own assets. Counted
# 2026-09-24: the hotels home page, a Goa listing and a flights listing
# each reference `mmtcdn.com` hundreds of times; the Akamai refusal and
# Chromium's own error page reference it 0 times. So a served page is
# positively identified rather than inferred from the absence of a marker,
# which also catches Chromium's network-error page wearing the site's
# hostname (§18).
OWN_ASSET_MARKER = "mmtcdn.com"
OWN_ASSET_MIN = 3

# A captcha widget's own loader, as the page would carry it if the site ever
# put one in front of a visitor. Counted 2026-09-24 on the hotels home page,
# a Goa listing and two flights listings, all fetched over the Scraping
# Browser: 0 of each. The bare words `captcha`, `recaptcha` and
# `cf-turnstile` are NOT markers: the Scraping Browser's auto-solve
# extension injects 16 hunter scripts that say them 21, 2 and 1 times on
# every page it loads (§24), and none of them loads one of these paths.
CAPTCHA_WIDGET_MARKERS = ("recaptcha/api.js", "recaptcha/enterprise.js",
                          "recaptcha/api2/anchor", "hcaptcha.com/1/api.js",
                          "challenges.cloudflare.com/turnstile")

# The site's own "nothing here" answers, inside HTTP 200 JSON. 400814 is
# "No Hotels Found" (an unknown city code, or a real one with nothing
# available); it is an answer about the listing, not a refused request.
EMPTY_ERROR_CODES = ("400814",)


# Akamai's THIRD answer: HTTP 200 and a body of exactly `200-OK`. Measured
# 2026-09-24 from a datacentre with Selenium's Chrome once its User-Agent
# was overridden over CDP (Network.setUserAgentOverride): 2 of 2 loads of
# /hotels/ came back as this 169-byte document, where the same Chrome
# without the override got its connection dropped. Counted 0 times on every
# served page and every API response captured. A browser wraps the raw text
# in its own `<pre>`, so the check is on the visible text of a short
# document, not on the bytes.
AKAMAI_DECOY_TEXT = "200-OK"
_TAG_RE = re.compile(r"<[^>]*>")


def _is_akamai_decoy(text: str) -> bool:
    if not text or len(text) > 2_000:
        return False
    return _TAG_RE.sub(" ", text).strip() == AKAMAI_DECOY_TEXT


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """The vendor whose refusal this is, or None."""
    head = html_lib.unescape((html or "")[:20_000])
    if any(m in head for m in AKAMAI_DENY_MARKERS) or _AKAMAI_REF_RE.search(head):
        return "akamai"
    if _is_akamai_decoy(head):
        return "akamai"
    return None


def _json_or_none(text: Optional[str]) -> Any:
    if not text:
        return None
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _as_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes)):
        got = _json_or_none(payload if isinstance(payload, str)
                            else payload.decode("utf-8", "replace"))
        return got if isinstance(got, dict) else {}
    return {}


def detect_page_state(text: Optional[str], status: Optional[int] = None,
                      url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See page_flow.STATE_POLICY.

        content    the listing API's JSON with properties in it, or a page
                   built from the site's own assets (the landing)
        empty      the API's JSON with none, or its "No Hotels Found"
        rejected   the API refused the PARAMETERS: an error envelope with
                   any other code (400108 "field name is not supported")
        blocked    Akamai's "Access Denied", or a 403
        challenge  a captcha widget's loader on a page the site did not
                   otherwise serve. Not observed here.
        throttled  429
        unknown    anything else

    Signals are ordered by what they PROVE (§17): the API's own envelope
    first, since no interstitial carries it; then Akamai's page; then the
    status; the asset count last. `waf_action` is accepted for the family's
    call signature and unused: this site's refusals carry no WAF header.
    """
    payload = _json_or_none(text)
    if isinstance(payload, dict):
        if "response" in payload and isinstance(payload["response"], dict):
            return "content" if count_rows(payload) > 0 else "empty"
        err = payload.get("error")
        if isinstance(err, dict):
            return "empty" if str(err.get("code")) in EMPTY_ERROR_CODES else "rejected"
    if detect_bot_challenge(text):
        return "blocked"
    own_assets = (text or "").count(OWN_ASSET_MARKER)
    if own_assets < OWN_ASSET_MIN and any(m in (text or "") for m in CAPTCHA_WIDGET_MARKERS):
        # A widget on a page the site did not otherwise serve. On a SERVED
        # page a widget guards nothing this repo reads (a login form, say),
        # and solving it would be paying for nothing (§8).
        return "challenge"
    if status == 403:
        return "blocked"
    if status == 429:
        return "throttled"
    if isinstance(payload, list):
        return "content"          # the autosuggest's answer
    if own_assets >= OWN_ASSET_MIN:
        return "content"
    return "unknown"


def api_error(text: Optional[str]) -> Optional[str]:
    """The endpoint's own complaint, for a `rejected` or `empty` response."""
    err = _as_dict(text).get("error")
    if not isinstance(err, dict):
        return None
    return "code %s: %s" % (err.get("code"),
                            err.get("alternateMessage") or err.get("message")
                            or "(no message)")


# ---------------------------------------------------------------------------
# Payload access
# ---------------------------------------------------------------------------

def _sections(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    resp = payload.get("response")
    if not isinstance(resp, dict):
        return []
    secs = resp.get("personalizedSections")
    return [s for s in secs if isinstance(s, dict)] if isinstance(secs, list) else []


def count_rows(payload: Any) -> int:
    return sum(len([h for h in (s.get("hotels") or []) if isinstance(h, dict)])
               for s in _sections(_as_dict(payload)))


def section_names(payload: Any) -> List[str]:
    """The names of the listing blocks holding properties, in order."""
    return [str(s.get("name")) for s in _sections(_as_dict(payload))
            if s.get("hotels")]


# The block that holds the city that was asked for. Anything else (the
# site's NEARBY_HOTELS) is a substitute.
OWN_SECTION = "RECOMMENDED_HOTELS"


def only_substitutes(payload: Any) -> bool:
    names = section_names(payload)
    return bool(names) and OWN_SECTION not in names


def next_cursor(payload: Any, previous: Optional[Cursor] = None) -> Optional[Cursor]:
    """The cursor for the page after this one, or None if it names none."""
    resp = _as_dict(payload).get("response")
    if not isinstance(resp, dict):
        return None
    last = _str(resp.get("lastHotelId"))
    if not last:
        return None
    shown = (previous.shown if previous else 0) + count_rows(payload)
    return Cursor(last_hotel_id=last, window=_str(resp.get("lastFetchedWindowInfo")) or "",
                  shown=shown)


def listing_ended(payload: Any) -> bool:
    """The site's own statement that nothing follows this page."""
    resp = _as_dict(payload).get("response")
    return isinstance(resp, dict) and resp.get("noMoreHotels") is True


def location_of(payload: Any) -> Dict[str, Optional[str]]:
    """The location the RESPONSE says it is about."""
    resp = _as_dict(payload).get("response")
    loc = resp.get("locationDetail") if isinstance(resp, dict) else None
    loc = loc if isinstance(loc, dict) else {}
    return {"code": _str(loc.get("id")), "name": _str(loc.get("name")),
            "country": _str(loc.get("countryId"))}


def currency_of(payload: Any) -> Optional[str]:
    resp = _as_dict(payload).get("response")
    cur = _str(resp.get("currency")) if isinstance(resp, dict) else None
    return cur.upper() if cur and re.fullmatch(r"[A-Za-z]{3}", cur) else None


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _positive(v: Any) -> Optional[float]:
    f = _float(v)
    return f if f is not None and f > 0 else None


def _https(url: Optional[str]) -> Optional[str]:
    """The site writes image addresses protocol-relative (`//r1imghtlak...`)."""
    if not url:
        return None
    return "https:" + url if url.startswith("//") else url


def discount_pct(price: Optional[float], original: Optional[float]) -> Optional[float]:
    """The discount the two figures imply, or None when they do not describe
    one (no original, or an original at or below the price)."""
    if price is None or original is None or original <= price:
        return None
    return round(100.0 * (original - price) / original, 1)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def parse_hotel(rec: Dict[str, Any], query: Query, *, section: Optional[str],
                currency: Optional[str], page: int, position: int) -> Optional[Hotel]:
    hid = _str(rec.get("id"))
    name = _str(rec.get("name"))
    if not hid or not name:
        return None
    pd = rec.get("priceDetail") if isinstance(rec.get("priceDetail"), dict) else {}
    price = _positive(pd.get("discountedPrice") if pd.get("discountedPrice") is not None
                      else pd.get("displayPrice"))
    listed = _positive(pd.get("price"))
    original = listed if (listed is not None and price is not None and listed > price) else None
    coupon = pd.get("coupon") if isinstance(pd.get("coupon"), dict) else {}
    rs = rec.get("reviewSummary") if isinstance(rec.get("reviewSummary"), dict) else {}
    rating_count = _int(rs.get("totalRatingCount"))
    rated = bool(rating_count) and _positive(rs.get("cumulativeRating")) is not None
    loc = rec.get("locationDetail") if isinstance(rec.get("locationDetail"), dict) else {}
    geo = rec.get("geoLocation") if isinstance(rec.get("geoLocation"), dict) else {}
    media = [m for m in (rec.get("media") or []) if isinstance(m, dict)]
    locality = rec.get("locationPersuasion")
    stars = _int(rec.get("starRating"))
    cats = [c for c in (rec.get("categories") or []) if isinstance(c, str)]
    return Hotel(
        url=_str(rec.get("detailDeeplinkUrl")) or "",
        sku=hid,
        title=name,
        price=price,
        original_price=original,
        currency=currency,
        discount_pct=discount_pct(price, original),
        price_with_tax=_positive(pd.get("discountedPriceWithTax")),
        price_with_fees=_positive(pd.get("discountedPriceWithTaxAndFees")
                                  or pd.get("discountedPriceWithTax")),
        price_basis=_str(rec.get("priceDisplayMsg")),
        coupon_code=_str(coupon.get("code")),
        coupon_discount=_positive(coupon.get("couponAmount")),
        property_type=_str(rec.get("propertyType")),
        star_rating=stars if stars and 1 <= stars <= 5 else None,
        rating=round(_float(rs.get("cumulativeRating")), 2) if rated else None,
        rating_count=rating_count if rated else None,
        review_count=_int(rs.get("totalReviewCount")) if rated else None,
        rating_text=_str(rs.get("ratingText")) if rated else None,
        review_source=_str(rs.get("source")) if rated else None,
        locality=_str(locality[0]) if isinstance(locality, list) and locality else None,
        city=_str(loc.get("name")),
        city_code=_str(loc.get("id")),
        country_code=_str(loc.get("countryId")),
        latitude=_float(geo.get("latitude")),
        longitude=_float(geo.get("longitude")),
        image=_https(_str(media[0].get("url"))) if media else None,
        categories=cats or None,
        sold_out=bool(rec.get("soldOut")) if rec.get("soldOut") is not None else None,
        sponsored=bool(rec.get("sponsored")) if rec.get("sponsored") is not None else None,
        section=section,
        check_in=query.checkin.isoformat() if query.checkin else None,
        check_out=query.checkout.isoformat() if query.checkout else None,
        adults=query.adults,
        rooms=len(query.rooms),
        page=page,
        position=position,
        mode="hotels",
        sort=query.sort,
        data_source="search-hotels",
    )


def parse_page(payload: Any, query: Query, page: int = 1) -> List[Hotel]:
    """Every property on one page of the listing, in the site's order.

    `position` counts the rows EMITTED on this page, not the slots in the
    payload, so a record the parser drops cannot shift every later
    position (§24).
    """
    p = _as_dict(payload)
    currency = currency_of(p)
    rows: List[Hotel] = []
    for sec in _sections(p):
        name = _str(sec.get("name"))
        for rec in sec.get("hotels") or []:
            if isinstance(rec, dict):
                row = parse_hotel(rec, query, section=name, currency=currency,
                                  page=page, position=len(rows) + 1)
                if row:
                    rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The server-rendered listing (the Scraper API's path)
# ---------------------------------------------------------------------------

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")


def initial_state_listing(html: Optional[str]) -> Optional[Dict[str, Any]]:
    """The listing a served listing PAGE carries in its own markup, in the
    API's envelope (`{"response": ...}`), or None if the page holds none.

    The page server-renders its first properties into
    `window.__INITIAL_STATE__.searchHotels`, in exactly the listing API's
    record shape: 5 properties, all priced, on two Goa pages fetched on
    2026-09-24 (one through the Scraping Browser, one through the Scraper
    API). That is the only data a client that can make no POST can read,
    and it is page 1's first five and nothing more: the cursor for anything
    after them is answered by the POST endpoint alone.
    """
    m = _INITIAL_STATE_RE.search(html or "")
    if not m:
        return None
    try:
        state, _end = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    listing = state.get("searchHotels") if isinstance(state, dict) else None
    if not isinstance(listing, dict) or not isinstance(listing.get("personalizedSections"), list):
        return None
    listing = dict(listing)
    # The server-rendered copy names its currency `searchHotelsCurrency`
    # where the API says `currency` (INR on both Goa pages). Read, not
    # defaulted: a page that states neither leaves the column null.
    if not listing.get("currency") and listing.get("searchHotelsCurrency"):
        listing["currency"] = listing["searchHotelsCurrency"]
    return {"response": listing}


def default_dates(today: Optional[date] = None, ahead_days: int = 30) -> Tuple[date, date]:
    """A one-night stay a month ahead: what the canary and the examples use,
    since a fixed date in a README rots into the past."""
    start = (today or date.today()) + timedelta(days=ahead_days)
    return start, start + timedelta(days=1)
