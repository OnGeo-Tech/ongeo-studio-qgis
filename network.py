"""REST requests through QGIS networking, including its proxy and TLS settings."""

import json
from urllib.parse import urlencode

from qgis.PyQt.QtCore import QByteArray, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsBlockingNetworkRequest


class QgisNetworkError(RuntimeError):
    """QGIS network transport error."""


class QgisNetworkTimeoutError(QgisNetworkError):
    """Network request timeout."""


class HttpResponse:
    """HTTP response used by REST and OAuth clients."""

    def __init__(self, status_code, content, headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = {str(name).lower(): str(value) for name, value in (headers or {}).items()}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def header(self, name, default=None):
        """Return an HTTP header without case sensitivity."""
        return self.headers.get(str(name).lower(), default)

    def json(self):
        return json.loads(self.content.decode("utf-8-sig"))


def _enum_value(owner, enum_name, value_name):
    """Return an enum member across PyQt5 and PyQt6 bindings."""
    enum = getattr(owner, enum_name, owner)
    return getattr(enum, value_name)


def _response_headers(reply):
    """Read raw response headers from QGIS' Python network reply wrapper."""
    headers = {}
    # QgsNetworkReplyContent.rawHeaderPairs() is not exposed to Python.
    # rawHeaderList() and rawHeader() are supported in QGIS 3 and QGIS 4.
    for name in reply.rawHeaderList():
        try:
            header_name = bytes(name).decode("ascii")
            header_value = bytes(reply.rawHeader(name)).decode("utf-8", errors="replace")
            headers[header_name] = header_value
        except (TypeError, UnicodeDecodeError, ValueError):
            continue
    return headers


def _request(method, url, headers, timeout_seconds, data=None):
    """Execute one HTTP request through QGIS networking."""
    request = QNetworkRequest(url)
    for name, value in (headers or {}).items():
        request.setRawHeader(name.encode("ascii"), str(value).encode("utf-8"))
    request.setRawHeader(b"Cache-Control", b"no-store")
    if timeout_seconds and hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout(int(timeout_seconds * 1000))

    blocking = QgsBlockingNetworkRequest()
    if method == "GET":
        error_code = blocking.get(request, True)
    else:
        request.setRawHeader(b"Content-Type", b"application/x-www-form-urlencoded")
        error_code = blocking.post(request, QByteArray(data), True)

    status_attribute = _enum_value(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute")
    if error_code == _enum_value(QgsBlockingNetworkRequest, "ErrorCode", "TimeoutError"):
        raise QgisNetworkTimeoutError("Network request timed out.")
    reply = blocking.reply()
    if reply is None:
        message = blocking.errorMessage() or "Network request returned no response."
        raise QgisNetworkError(message)
    if reply.error() == _enum_value(QNetworkReply, "NetworkError", "TimeoutError"):
        raise QgisNetworkTimeoutError("Network request timed out.")
    if (
        timeout_seconds
        and reply.error()
        == _enum_value(QNetworkReply, "NetworkError", "OperationCanceledError")
        and reply.attribute(status_attribute) is None
    ):
        raise QgisNetworkTimeoutError("Network request timed out.")

    raw_status = reply.attribute(status_attribute)
    try:
        status_code = int(raw_status)
    except (TypeError, ValueError):
        status_code = 0

    # Qt may report an HTTP 4xx/5xx response as a transport error.
    if status_code >= 400:
        return HttpResponse(status_code, bytes(reply.content()), _response_headers(reply))

    if error_code != _enum_value(QgsBlockingNetworkRequest, "ErrorCode", "NoError"):
        raise QgisNetworkError(blocking.errorMessage() or reply.errorString() or "Network request failed.")
    if not status_code:
        raise QgisNetworkError("Network response has no HTTP status.")
    return HttpResponse(status_code, bytes(reply.content()), _response_headers(reply))


def get(url, headers=None, params=None, timeout_seconds=None):
    """Fetch a GET response through QGIS networking."""
    qurl = QUrl(url)
    if params:
        query = QUrlQuery(qurl)
        for name, value in params.items():
            query.addQueryItem(str(name), str(value))
        qurl.setQuery(query)
    return _request("GET", qurl, headers, timeout_seconds)


def post_form(url, data, headers=None, timeout_seconds=None):
    """Send an OAuth form through QGIS networking."""
    body = urlencode(data).encode("utf-8")
    return _request("POST", QUrl(url), headers, timeout_seconds, body)
