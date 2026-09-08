"""Endpoint constants for the Exceleron MyUsage portal.

Everything here was verified against a live OUC postpaid solar account on
2026-09-07 (see docs/probes.md once M0 runs). MyUsage is a ColdFusion app;
there is no documented API and none of this is guaranteed stable.
"""

from __future__ import annotations

BASE_URL = "https://www.myusage.com"
LOGIN_URL = f"{BASE_URL}/login"
DATA_URL = f"{BASE_URL}/data.cfm"

# Browser-consistent UA (opower precedent: match how the platform is normally
# used). Documented in the README alongside the once-daily cadence rationale.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Headers for ordinary page loads.
HTML_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# The login POST is an XHR endpoint returning JSON. myusage-ha logs in with
# exactly these headers and no priming GET; the 2026-09-07 probe saw HTTP 500
# on a cold POST without them. M0's auth matrix isolates the load-bearing part.
LOGIN_XHR_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL + "/",
}

# Usage-history surface (GET). appFlow comes from the post-login landing URL.
HISTORY_QUERY = {
    "appPage": "Postpaid",
    "appPageScreen": "History",
    "appPageScreenSub": "Usage History",
}

TRANSITION_15MIN = "View 15 Minute Usage"
TRANSITION_HOURLY = "View Hourly Usage"

# Table ids observed in the rendered pages.
GRID_DAILY_ID = "gridUsageHistory"
GRID_15MIN_ID = "grid15MinuteUsage"
GRID_HOURLY_ID = "gridHourlyUsage"

# ColdFusion CSRF hidden-input names used by the history date-range POST.
CSRF_FIELDS = ("cf_CSRFToken", "cf_CSRFToken_web")

# The cfform client-side validation descriptor the portal expects verbatim on
# the history POST (sourced from myusage-ha; proven for Water, probed for
# Electric in M0).
SUBMIT_VALIDATE_TIME_PERIOD = (
    "[['FromDate','isRequired','','From:','0'],"
    "['FromDate','isDate','','From:','0'],"
    "['ToDate','isRequired','','To:','0'],"
    "['ToDate','isDate','','To:','0'],"
    "['ServiceType','isRequired','','Service:','0']]"
)

REQUEST_TIMEOUT_S = 30.0

# Known size of the useless stub page returned for unrecognized transitions
# (observed 2026-09-07: exactly 4842 bytes, no grid tables).
STUB_SIZE_HINT = 4842
