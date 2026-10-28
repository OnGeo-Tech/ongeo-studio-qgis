"""OnGeo REST API requests."""

from urllib.parse import quote

from .config import (
    AUTOCOMPLETE_URL,
    API_DETAILS_TIMEOUT_SECONDS,
    API_LOCATE_TIMEOUT_SECONDS,
    API_SEARCH_TIMEOUT_SECONDS,
    INFO_PANEL_URL,
    PARCEL_DETAILS_URL,
    PARCEL_LOCATE_URL,
    QGIS_CLIENT_HEADER,
    QGIS_CLIENT_HEADER_VALUE,
    SEARCH_MIN_CHARS,
)
from .network import QgisNetworkError, QgisNetworkTimeoutError, get


class OnGeoApiError(Exception):
    """API error with a user-facing message and optional HTTP status."""

    def __init__(self, message, status_code=None, retry_after_seconds=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _retry_after_seconds(response):
    """Return a numeric Retry-After value when supplied by the API."""
    try:
        return max(0, int(response.header("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def _headers(access_token=None):
    """Build REST request headers."""
    headers = {
        "Accept": "application/json",
        QGIS_CLIENT_HEADER: QGIS_CLIENT_HEADER_VALUE,
    }

    if access_token:
        headers["Authorization"] = "Bearer {}".format(access_token)

    return headers


def _raise_api_error(resp):
    """Convert HTTP failures into user-facing API errors."""
    if resp.status_code == 401:
        raise OnGeoApiError("Authentication expired.", status_code=401)
    if resp.status_code == 403:
        raise OnGeoApiError("You do not have access to this service.", status_code=403)
    if resp.status_code == 429:
        raise OnGeoApiError(
            "Your request limit has been reached.",
            status_code=429,
            retry_after_seconds=_retry_after_seconds(resp),
        )
    if resp.status_code == 404:
        raise OnGeoApiError("Requested OnGeo resource was not found.", status_code=404)
    if resp.status_code == 400:
        raise OnGeoApiError("Invalid OnGeo request.", status_code=400)
    if resp.status_code >= 500:
        raise OnGeoApiError("OnGeo service is temporarily unavailable.", status_code=resp.status_code)
    if not resp.ok:
        raise OnGeoApiError("OnGeo request failed. Please try again.", status_code=resp.status_code)


def _get_json(url, access_token, read_timeout, **kwargs):
    """Send a REST request and report network failures clearly."""
    try:
        resp = get(
            url,
            headers=_headers(access_token),
            timeout_seconds=read_timeout,
            **kwargs,
        )
    except (QgisNetworkTimeoutError, QgisNetworkError) as exc:
        raise OnGeoApiError("OnGeo service is temporarily unavailable.") from exc
    _raise_api_error(resp)
    try:
        return resp.json()
    except (UnicodeError, ValueError) as exc:
        raise OnGeoApiError("Unexpected response from OnGeo.") from exc


def fetch_info_panel_html():
    """Fetch the CMS HTML shown in the OnGeo information dialog."""
    data = _get_json(INFO_PANEL_URL, None, API_DETAILS_TIMEOUT_SECONDS)
    try:
        content = data["qgis.info-panel"]["en"]
    except (KeyError, TypeError) as exc:
        raise OnGeoApiError("Unexpected OnGeo information response.") from exc
    if not isinstance(content, str) or not content.strip():
        raise OnGeoApiError("Unexpected OnGeo information response.")
    return content


def search_parcels(query, access_token=None):
    """Fetch parcel search suggestions."""
    query = (query or "").strip()
    if len(query) < SEARCH_MIN_CHARS:
        return []

    data = _get_json(
        AUTOCOMPLETE_URL,
        access_token,
        API_SEARCH_TIMEOUT_SECONDS,
        params={"query": query},
    )
    return data if isinstance(data, list) else []


def fetch_parcel_details(parcel_id, access_token=None):
    """Fetch parcel geometry and details by identifier."""
    parcel_id = (parcel_id or "").strip()
    if not parcel_id:
        raise ValueError("Missing parcel identifier.")

    data = _get_json(
        PARCEL_DETAILS_URL.format(parcel_id=quote(parcel_id, safe="")),
        access_token,
        API_DETAILS_TIMEOUT_SECONDS,
    )
    if not isinstance(data, dict):
        raise ValueError("Unexpected parcel details response.")
    return data


def locate_parcel(latitude, longitude, access_token=None):
    """Fetch parcel information for a WGS 84 map location."""
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid map location.") from exc

    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Map location is outside WGS 84 bounds.")

    data = _get_json(
        PARCEL_LOCATE_URL,
        access_token,
        API_LOCATE_TIMEOUT_SECONDS,
        params={"lat": latitude, "lon": longitude},
    )
    if not isinstance(data, dict):
        raise ValueError("Unexpected parcel location response.")
    return data
