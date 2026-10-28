"""OAuth 2.0 Authorization Code flow with PKCE for OnGeo."""

import base64
import binascii
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .config import (
    KEYCLOAK_AUTH_URL,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_SCOPE,
    KEYCLOAK_TOKEN_URL,
    AUTH_TOKEN_DEFAULT_LIFETIME_SECONDS,
    AUTH_TOKEN_TIMEOUT_SECONDS,
    QGIS_AUTH_LOOPBACK_HOST,
    QGIS_AUTH_LOOPBACK_PATH,
    QGIS_AUTH_LOOPBACK_PORT,
    QGIS_AUTH_LOGIN_TIMEOUT_SECONDS,
)
from .network import QgisNetworkError, post_form


def _claims_from_jwt(token):
    """Read JWT claims for local UI state; this does not verify the token."""
    if not isinstance(token, str) or len(token) > 65536:
        return {}
    try:
        parts = token.split(".")
        if len(parts) != 3 or len(parts[1]) > 32768:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, TypeError, UnicodeError, OverflowError, binascii.Error):
        return {}


def _display_name_from_jwt(token):
    """Read display-only profile claims; this is not token verification."""
    try:
        claims = _claims_from_jwt(token)

        def _clean(value):
            if not isinstance(value, str):
                return ""
            return " ".join(value.split())[:80]

        given = _clean(claims.get("given_name"))
        family = _clean(claims.get("family_name"))
        return " ".join(part for part in (given, family) if part)
    except (TypeError, AttributeError):
        return ""


def _roles_from_jwt(token):
    """Return realm and client role names declared in an access token."""
    claims = _claims_from_jwt(token)
    roles = set()

    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        realm_roles = realm_access.get("roles")
        if isinstance(realm_roles, (list, tuple, set)):
            roles.update(role for role in realm_roles if isinstance(role, str))

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        for resource in resource_access.values():
            if not isinstance(resource, dict):
                continue
            resource_roles = resource.get("roles")
            if isinstance(resource_roles, (list, tuple, set)):
                roles.update(
                    role for role in resource_roles if isinstance(role, str)
                )
    return frozenset(roles)


class AuthTransientError(RuntimeError):
    """A temporary authentication service or network failure."""


class AuthRejectedError(RuntimeError):
    """A token was rejected and the user must authenticate again."""


def _callback_path_matches(path):
    """Check whether a request targets the OAuth callback path."""
    path_part = QGIS_AUTH_LOOPBACK_PATH.strip("/")
    expected = "/" + path_part if path_part else "/"
    current = (path or "").rstrip("/") or "/"
    return current == expected


def _redirect_uri_for_port(port):
    """Build a redirect URI using the assigned loopback port."""
    base_url = "http://{}:{}".format(
        QGIS_AUTH_LOOPBACK_HOST,
        port,
    )
    path_part = QGIS_AUTH_LOOPBACK_PATH.strip("/")
    if path_part:
        return "{}/{}".format(base_url, path_part)
    return base_url


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """Serve OAuth callbacks on the local loopback interface."""

    allow_reuse_address = True


def _serve_loopback(server):
    """Handle loopback requests until cancellation without blocking the UI thread."""
    while not server.stop_event.is_set():
        try:
            server.handle_request()
        except OSError:
            if not server.stop_event.is_set():
                raise


class _LoopbackCallbackHandler(BaseHTTPRequestHandler):
    """Receive one OAuth callback from the system browser."""

    server_version = "OnGeoQGISAuth/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        # The callback URL contains an authorization code and must not be logged.
        return

    def _send_html(self, status_code, title, message):
        body = (
            "<!doctype html>"
            '<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>{}</title></head>"
            '<body style="font-family:Arial,sans-serif;padding:32px;color:#222">'
            "<h2>{}</h2><p>{}</p>"
            "</body></html>"
        ).format(title, title, message).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_silent_success(self):
        success_url = "https://ongeo.us/#msg=Login%20completed.%20Return%20to%20QGIS."
        body = (
            "<!doctype html>"
            '<html><head>'
            '<meta http-equiv="refresh" content="0; url={0}">'
            "</head></html>"
        ).format(success_url).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        client = getattr(self.server, "auth_client", None)
        if client is None:
            self._send_html(500, "OnGeo sign in", "Authentication listener is unavailable.")
            return

        parsed = urlparse(self.path)
        if not _callback_path_matches(parsed.path):
            self.send_response(204)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        state = (params.get("state") or [None])[0]
        code = (params.get("code") or [None])[0]
        error = (params.get("error") or [None])[0]
        error_description = (params.get("error_description") or [None])[0]

        # Ignore requests without OAuth parameters; they do not complete login.
        if not state and not code and not error:
            self._send_html(404, "OnGeo sign in", "Callback parameters are missing.")
            return

        if not client._matches_state(state):
            self._send_html(400, "OnGeo sign in", "Invalid authentication state.")
            return

        if error:
            client._set_callback_result(
                error=error,
                error_description=error_description,
            )
            self._send_html(200, "Sign in cancelled", "Authentication was not completed.")
            self._shutdown_server_async()
            return

        if not code:
            client._set_callback_result(error="missing_code")
            self._send_html(400, "OnGeo sign in", "Authorization code is missing.")
            self._shutdown_server_async()
            return

        client._set_callback_result(code=code)
        self._send_silent_success()
        self._shutdown_server_async()

    def _shutdown_server_async(self):
        self.server.stop_event.set()


class OnGeoAuthClient:
    """Manage OAuth tokens and the loopback callback."""

    def __init__(self):
        self._access_token = None
        self._refresh_token = None
        self._access_token_expires_at = 0.0
        self._user_display_name = ""
        self._access_roles = frozenset()

        self.state = None
        self.code_verifier = None
        self.code_challenge = None
        self.redirect_uri = None

        self._server = None
        self._callback_lock = threading.Lock()
        self._callback_result = None
        self._callback_consumed = False
        self._login_started_at = 0.0

    def is_authenticated(self):
        """Return whether an access token is available."""
        return bool(self._access_token)

    def user_display_name(self):
        """Return the display name from the current authenticated session."""
        return self._user_display_name

    def access_token(self):
        """Return the current access token, if any."""
        return self._access_token

    def refresh_token(self):
        """Return the refresh token, if any."""
        return self._refresh_token

    def has_subscriber_role(self):
        """Return whether the current access token grants OnGeo subscription access."""
        return "subscriber" in self._access_roles

    def access_token_expired(self, skew_seconds=30):
        """Check whether the access token has expired or will expire soon."""
        if not self._access_token:
            return True
        if not self._access_token_expires_at:
            return False
        return time.time() >= (self._access_token_expires_at - skew_seconds)

    def login_in_progress(self):
        """Return whether the callback listener is waiting for a login."""
        return self._server is not None and self._callback_result is None

    def login_timed_out(self):
        """Check whether the current login attempt has timed out."""
        if not self._login_started_at or self._callback_result is not None:
            return False
        return (time.time() - self._login_started_at) >= QGIS_AUTH_LOGIN_TIMEOUT_SECONDS

    def prepare_login(self):
        """Create the OAuth state and PKCE S256 challenge."""
        self.state = secrets.token_urlsafe(32)
        self.code_verifier = secrets.token_urlsafe(64)

        digest = hashlib.sha256(
            self.code_verifier.encode("ascii")
        ).digest()

        self.code_challenge = (
            base64.urlsafe_b64encode(digest)
            .rstrip(b"=")
            .decode("ascii")
        )

    def start_login(self):
        """Start the loopback listener and return the authorization URL."""
        self.cancel_login(clear_pkce=False)
        self.prepare_login()

        with self._callback_lock:
            self._callback_result = None
            self._callback_consumed = False

        try:
            server = _LoopbackHTTPServer(
                (QGIS_AUTH_LOOPBACK_HOST, QGIS_AUTH_LOOPBACK_PORT),
                _LoopbackCallbackHandler,
            )
        except OSError as exc:
            raise RuntimeError(
                "Could not start local authentication listener on {}: {}".format(
                    QGIS_AUTH_LOOPBACK_HOST,
                    exc,
                )
            )

        server.daemon_threads = True
        server.auth_client = self
        server.stop_event = threading.Event()
        server.timeout = 0.2

        self.redirect_uri = _redirect_uri_for_port(server.server_address[1])
        self._server = server
        self._login_started_at = time.time()

        server_thread = threading.Thread(
            target=_serve_loopback,
            args=(server,),
            name="OnGeoOAuthLoopback",
            daemon=True,
        )
        server_thread.start()

        return self.authorization_url()

    def authorization_url(self):
        """Build the Keycloak authorization URL for the current login."""
        if not self.state or not self.code_challenge or not self.redirect_uri:
            raise RuntimeError("Authentication listener is not started.")

        params = {
            "client_id": KEYCLOAK_CLIENT_ID,
            "response_type": "code",
            "scope": KEYCLOAK_SCOPE,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
        }
        return KEYCLOAK_AUTH_URL + "?" + urlencode(params)

    def consume_callback_result(self):
        """Consume the callback result at most once."""
        with self._callback_lock:
            if self._callback_result is None or self._callback_consumed:
                return None
            self._callback_consumed = True
            return dict(self._callback_result)

    def token_exchange_payload(self, authorization_code):
        """Build the authorization code exchange payload."""
        if not authorization_code:
            raise ValueError("Missing authorization code.")
        if not self.code_verifier or not self.redirect_uri:
            raise RuntimeError("PKCE login state is missing.")
        return {
            "grant_type": "authorization_code",
            "client_id": KEYCLOAK_CLIENT_ID,
            "code": authorization_code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": self.code_verifier,
        }

    def exchange_code_for_tokens(self, authorization_code):
        """Exchange the authorization code for tokens."""
        return self._request_tokens(self.token_exchange_payload(authorization_code))

    def refresh_access_token(self):
        """Refresh the access token."""
        if not self._refresh_token:
            raise RuntimeError("Refresh token is not available.")

        return self._request_tokens({
            "grant_type": "refresh_token",
            "client_id": KEYCLOAK_CLIENT_ID,
            "refresh_token": self._refresh_token,
        })

    def _request_tokens(self, payload):
        """Send a token request to Keycloak."""
        try:
            response = post_form(
                KEYCLOAK_TOKEN_URL,
                payload,
                headers={"Accept": "application/json"},
                timeout_seconds=AUTH_TOKEN_TIMEOUT_SECONDS,
            )
            return self._token_response(response)
        except QgisNetworkError as exc:
            raise AuthTransientError("Keycloak request temporarily unavailable.") from exc

    def apply_token_response(self, token_data):
        """Keep OAuth tokens in QGIS process memory."""
        token_data = token_data or {}
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Keycloak response does not contain access_token.")

        self._access_token = access_token
        self._access_roles = _roles_from_jwt(access_token)
        display_name = (
            _display_name_from_jwt(token_data.get("id_token"))
            or _display_name_from_jwt(access_token)
        )
        if display_name:
            self._user_display_name = display_name
        if token_data.get("refresh_token"):
            self._refresh_token = token_data.get("refresh_token")

        expires_in = token_data.get("expires_in")
        try:
            expires_in = int(expires_in or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            expires_in = AUTH_TOKEN_DEFAULT_LIFETIME_SECONDS
        self._access_token_expires_at = (
            time.time() + expires_in
        )

        self.cancel_login(clear_pkce=True)

    def cancel_login(self, clear_pkce=True):
        """Stop the local OAuth callback listener."""
        server = self._server
        self._server = None
        self._login_started_at = 0.0

        if server is not None:
            server.stop_event.set()
            try:
                server.server_close()
            except OSError:
                pass

        if clear_pkce:
            self.state = None
            self.code_verifier = None
            self.code_challenge = None
            self.redirect_uri = None

    def logout(self):
        """Clear the login state and tokens."""
        self.cancel_login(clear_pkce=True)
        self._access_token = None
        self._refresh_token = None
        self._access_token_expires_at = 0.0
        self._user_display_name = ""
        self._access_roles = frozenset()
        with self._callback_lock:
            self._callback_result = None
            self._callback_consumed = False

    def _matches_state(self, returned_state):
        if not self.state or not returned_state:
            return False
        try:
            return secrets.compare_digest(
                self.state.encode("ascii"), returned_state.encode("ascii")
            )
        except UnicodeEncodeError:
            return False

    def _set_callback_result(self, code=None, error=None, error_description=None):
        with self._callback_lock:
            if self._callback_result is not None:
                return
            self._callback_result = {
                "code": code,
                "error": error,
                "error_description": error_description,
            }

    @staticmethod
    def _token_response(response):
        try:
            data = response.json()
        except ValueError:
            data = {}

        if not isinstance(data, dict):
            raise AuthTransientError("Unexpected Keycloak token response.")
        if not response.ok:
            message = data.get("error_description") or data.get("error")
            if not message:
                message = "HTTP {}".format(response.status_code)
            error_type = (
                AuthRejectedError
                if response.status_code in (400, 401)
                else AuthTransientError
            )
            raise error_type("Keycloak token request failed: {}".format(message))
        return data
