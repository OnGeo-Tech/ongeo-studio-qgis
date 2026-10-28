"""OnGeo US Parcel Explorer QGIS plugin."""

import logging
import os
import re
import time
from math import log2

from qgis.PyQt.QtCore import Qt, QSize, QTimer, QUrl, QUrlQuery
from qgis.PyQt.QtGui import QDesktopServices, QIcon
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolBar,
    QToolButton,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsDataProvider,
    QgsLabelingEngineSettings,
    QgsMessageLog,
    QgsNetworkAccessManager,
    QgsProject,
    QgsTask,
    QgsVectorTileLayer,
)

from .api import (
    OnGeoApiError,
    fetch_info_panel_html,
    fetch_parcel_details,
    locate_parcel,
    search_parcels,
)
from .auth import AuthRejectedError, AuthTransientError, OnGeoAuthClient
from .config import (
    PARCEL_TILE_MAX_ZOOM,
    PARCEL_TILE_MIN_SCALE,
    PARCEL_TILE_MVT_URL_TEMPLATE,
    PARCEL_TILE_SOURCE_MIN_ZOOM,
    QGIS_AUTH_CALLBACK_POLL_MS,
    QGIS_AUTH_REFRESH_CHECK_MS,
    QGIS_CLIENT_HEADER,
    QGIS_CLIENT_HEADER_VALUE,
    SEARCH_DEBOUNCE_MS,
    SEARCH_DISPLAY_LIMIT,
    SEARCH_MIN_CHARS,
    SEARCH_RATE_LIMIT_RETRY_SECONDS,
)
from .dialogs import (
    ICON_PATH,
    PLUGIN_NAME,
    set_full_logo,
    show_ongeo_info,
    show_result_info,
    show_subscription_required,
)
from .parcel_tiles import parcel_tile_labeling, parcel_tile_renderer
from .qt_compat import CURSOR_POINTING_HAND, MESSAGE_INFO, MESSAGE_WARNING, TEXT_PLAIN
from .layers import (
    PARCEL_LAYER_PROPERTY,
    append_parcel_to_layer,
    place_layer_at_top,
    result_compact_text,
    zoom_to_geometry,
)
from .map_tools import ParcelIdentifyTool
from .ui import STYLE, ParcelResultsPopup


TOOLBAR_OBJECT_NAME = "OnGeoToolbar"
LEGACY_TOOLBAR_OBJECT_NAME = "OnGeoUSParcelSearch"
TOOLBAR_ACTION_PREFIX = "OnGeoUSParcelExplorer"
SEARCH_INPUT_OBJECT_NAME = "OnGeoSearchInput"
SEARCH_CACHE_LIMIT = 10
SUBSCRIPTION_DIALOG_REFRESH_MS = 5000
SUBSCRIPTION_BACKGROUND_REFRESH_MS = 30000
PARCEL_TILE_QGIS_LAYER_NAME = "OnGeo USA Parcel Layer"
PARCEL_TILE_LAYER_PROPERTY = "ongeo_us_parcel_tile_layer"
PLUGIN_DIR = os.path.dirname(__file__)
TOOL_BUTTON_ICON_SIZE = QSize(16, 16)
PARCEL_LAYER_SHOW_ICON_BASE_PATH = os.path.join(PLUGIN_DIR, "icons", "parcel_layer_show")
PARCEL_LAYER_HIDE_ICON_BASE_PATH = os.path.join(PLUGIN_DIR, "icons", "parcel_layer_hide")
LOGIN_ICON_BASE_PATH = os.path.join(PLUGIN_DIR, "icons", "login")
LOGOUT_ICON_BASE_PATH = os.path.join(PLUGIN_DIR, "icons", "logout")
IDENTIFY_INACTIVE_ICON_BASE_PATH = os.path.join(PLUGIN_DIR, "icons", "identify_start")
IDENTIFY_ACTIVE_ICON_BASE_PATH = os.path.join(PLUGIN_DIR, "icons", "identify_stop")
USER_STATUS_ICON_PATH = os.path.join(PLUGIN_DIR, "icons", "user_verified.svg")
USER_NAME_MAX_WIDTH = 150
USER_STATUS_ICON_SIZE = QSize(28, 24)
# Reserve this width even before sign in so the toolbar does not resize.
USER_IDENTITY_WIDTH = 191


def _stateful_tool_icon(icon_base_path, active=False):
    """Provide explicit SVG colors for normal, active and disabled modes."""
    base_path, _ = os.path.splitext(icon_base_path)
    normal_path = "{}_{}.svg".format(
        base_path, "active" if active else "available"
    )
    icon = QIcon(normal_path)
    disabled_mode = getattr(getattr(QIcon, "Mode", QIcon), "Disabled")
    icon.addFile("{}_disabled.svg".format(base_path), QSize(), disabled_mode)
    return icon


WEB_MERCATOR_WORLD_WIDTH = 40075016.68557849
WEB_MERCATOR_TILE_SIZE = 256
PARCEL_TILE_PATH_PATTERN = re.compile(r"(?:^|/)\d+/\d+/\d+(?:\.[A-Za-z0-9]+)?/?$")


def _quiet_task_flags():
    """Use silent task notifications where the installed QGIS supports them."""
    flags = getattr(QgsTask, "Flag", QgsTask)
    return getattr(flags, "Silent", getattr(flags, "CanCancel"))


def _task_error_result(exception, **result):
    """Convert a task exception into a consistent result dictionary."""
    api_error = isinstance(exception, OnGeoApiError)
    result.update(
        {
            "error": str(exception),
            "status_code": exception.status_code if api_error else None,
            "user_message": str(exception) if api_error else None,
            "retry_after_seconds": exception.retry_after_seconds if api_error else None,
        }
    )
    return result


class OnGeoUSParcelSearchPlugin(object):
    """Manage the OnGeo toolbar, authentication, and parcel layers."""

    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self._toolbar_actions = []
        self._created_toolbar = False
        self.logo_label = None
        self.menu_action = None
        self.search_edit = None
        self.results_popup = None
        self.info_panel_task = None
        self.info_panel_generation = 0
        self.parcel_layer_button = None
        self.identify_button = None
        self.parcel_identify_tool = None
        self.identify_task = None
        self.identify_generation = 0
        self._previous_map_tool = None
        self._map_tool_signal_connected = False
        self._map_scale_signal_connected = False
        self._map_extent_signal_connected = False
        self._parcel_tile_scale_message = None
        self._parcel_tile_scale_message_visible = False
        self._parcel_identify_message = None
        self._parcel_identify_message_visible = False
        self.account_button = None
        self.user_identity = None
        self.user_name_label = None
        self.user_status_icon = None
        self.search_timer = None
        self.search_task = None
        self.add_task = None
        self.search_generation = 0
        self.add_generation = 0
        self._add_in_progress = False
        self.current_query = ""
        self.add_first_generation = None
        self.results = []
        self.search_cache = {}
        self.search_cache_order = []
        self.search_cooldown_until = 0.0
        self.parcel_layer = None
        self.parcel_tile_layer = None
        self.auth_client = OnGeoAuthClient()
        self.auth_callback_timer = None
        self.auth_refresh_timer = None
        self.subscription_refresh_timer = None
        self.auth_exchange_task = None
        self.auth_refresh_task = None
        self.auth_refresh_warning_shown = False
        self.auth_generation = 0
        self._auth_retry_callbacks = []
        self.subscription_dialog = None
        self.network_request_preprocessor_id = None
        self.network_request_preprocessor = None
        self._partial_labels_before_ongeo = None
        self._label_project_signals_connected = False
        self._label_order_signals_connected = False

    def initGui(self):
        """Create the OnGeo toolbar."""
        self.toolbar = self._get_or_create_toolbar()
        self.toolbar.setStyleSheet(STYLE)
        self._remove_existing_toolbar_controls()

        self.logo_label = QLabel()
        self.logo_label.setObjectName("OnGeoLogoLabel")
        self.logo_label.setToolTip("About {}".format(PLUGIN_NAME))
        self.logo_label.setCursor(CURSOR_POINTING_HAND)
        self.logo_label.mousePressEvent = self._show_ongeo_info
        set_full_logo(self.logo_label, 22)
        self._add_toolbar_widget(self.logo_label, "Logo")
        self._add_toolbar_separator()

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName(SEARCH_INPUT_OBJECT_NAME)
        self.search_edit.setPlaceholderText("Search USA parcel or address")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._on_search_return_pressed)
        self._add_toolbar_widget(self.search_edit, "Search")
        self._add_toolbar_separator()

        self.parcel_layer_button = QToolButton()
        self.parcel_layer_button.setObjectName("OnGeoParcelLayerButton")
        self.parcel_layer_button.setIcon(_stateful_tool_icon(PARCEL_LAYER_SHOW_ICON_BASE_PATH))
        self.parcel_layer_button.setIconSize(TOOL_BUTTON_ICON_SIZE)
        self.parcel_layer_button.setToolTip("Show US parcels")
        self.parcel_layer_button.setCheckable(True)
        self.parcel_layer_button.clicked.connect(self._on_parcel_layer_button_clicked)
        self._add_toolbar_widget(self.parcel_layer_button, "ParcelLayer")
        self._add_toolbar_separator()

        self.identify_button = QToolButton()
        self.identify_button.setObjectName("OnGeoIdentifyButton")
        self.identify_button.setIcon(_stateful_tool_icon(IDENTIFY_INACTIVE_ICON_BASE_PATH))
        self.identify_button.setIconSize(TOOL_BUTTON_ICON_SIZE)
        self.identify_button.setToolTip("Enable parcel identification")
        self.identify_button.setCheckable(True)
        self.identify_button.clicked.connect(self._on_identify_button_clicked)
        self._add_toolbar_widget(self.identify_button, "Identify")
        self._add_toolbar_separator()

        self.account_button = QToolButton()
        self.account_button.setObjectName("OnGeoAccountButton")
        self.account_button.setIcon(_stateful_tool_icon(LOGIN_ICON_BASE_PATH))
        self.account_button.setIconSize(TOOL_BUTTON_ICON_SIZE)
        self.account_button.setToolTip("Sign in to OnGeo")
        self.account_button.setCheckable(True)
        self.account_button.setAutoRaise(False)
        self.account_button.clicked.connect(self._on_account_clicked)
        self._add_toolbar_widget(self.account_button, "Account")

        # Keep the user area in the toolbar at all times, even while signed out.
        # Only its contents change; the outer toolbar width stays stable.
        self.user_identity = QWidget()
        self.user_identity.setObjectName("OnGeoUserIdentity")
        self.user_identity.setFixedWidth(USER_IDENTITY_WIDTH)
        user_layout = QHBoxLayout(self.user_identity)
        user_layout.setContentsMargins(3, 0, 5, 0)
        user_layout.setSpacing(5)

        self.user_status_icon = QLabel()
        self.user_status_icon.setObjectName("OnGeoUserStatusIcon")
        self.user_status_icon.setFixedSize(USER_STATUS_ICON_SIZE)
        self.user_status_icon.setPixmap(
            QIcon(USER_STATUS_ICON_PATH).pixmap(USER_STATUS_ICON_SIZE)
        )
        user_layout.addWidget(self.user_status_icon)

        self.user_name_label = QLabel()
        self.user_name_label.setObjectName("OnGeoUserName")
        self.user_name_label.setTextFormat(TEXT_PLAIN)
        self.user_name_label.setFixedWidth(USER_NAME_MAX_WIDTH)
        self.user_name_label.setWordWrap(False)
        user_layout.addWidget(self.user_name_label)

        # Put the icon and full name directly after the login/logout button.
        self._add_toolbar_widget(self.user_identity, "UserIdentity")
        self.user_status_icon.hide()
        self._install_network_request_preprocessor()
        self._update_auth_ui()
        self._clear_auth_sensitive_ui()
        project = QgsProject.instance()
        project.layersAdded.connect(self._sync_label_edge_policy)
        project.layerRemoved.connect(self._on_project_layer_removed)
        project.layersAdded.connect(self._sync_identify_button_state)
        self._label_project_signals_connected = True
        self._sync_label_edge_policy()
        root = project.layerTreeRoot()
        root.layerOrderChanged.connect(self._sync_selected_parcel_labels)
        root.visibilityChanged.connect(self._sync_selected_parcel_labels)
        root.visibilityChanged.connect(self._sync_identify_button_state)
        root.visibilityChanged.connect(self._sync_parcel_tile_scale_message)
        root.visibilityChanged.connect(self._sync_parcel_layer_button_state)
        root.addedChildren.connect(self._sync_identify_button_state)
        root.addedChildren.connect(self._sync_parcel_layer_button_state)
        root.removedChildren.connect(self._sync_identify_button_state)
        root.removedChildren.connect(self._sync_parcel_layer_button_state)
        self._label_order_signals_connected = True
        self._sync_selected_parcel_labels()
        self._sync_identify_button_state()
        self._sync_parcel_layer_button_state()
        canvas = self.iface.mapCanvas()
        scale_changed = getattr(canvas, "scaleChanged", None)
        if scale_changed is not None:
            scale_changed.connect(self._sync_parcel_tile_scale_message)
            self._map_scale_signal_connected = True
        extents_changed = getattr(canvas, "extentsChanged", None)
        if extents_changed is not None:
            extents_changed.connect(self._sync_parcel_tile_scale_message)
            self._map_extent_signal_connected = True
        self._sync_parcel_tile_scale_message()
        map_tool_set = getattr(canvas, "mapToolSet", None)
        if map_tool_set is not None:
            map_tool_set.connect(self._on_map_tool_set)
            self._map_tool_signal_connected = True
        self.auth_callback_timer = QTimer()
        self.auth_callback_timer.setSingleShot(False)
        self.auth_callback_timer.setInterval(QGIS_AUTH_CALLBACK_POLL_MS)
        self.auth_callback_timer.timeout.connect(self._check_auth_callback)

        self.auth_refresh_timer = QTimer()
        self.auth_refresh_timer.setSingleShot(False)
        self.auth_refresh_timer.setInterval(QGIS_AUTH_REFRESH_CHECK_MS)
        self.auth_refresh_timer.timeout.connect(self._check_auth_refresh)
        self.auth_refresh_timer.start()

        self.subscription_refresh_timer = QTimer()
        self.subscription_refresh_timer.setSingleShot(False)
        self.subscription_refresh_timer.setInterval(SUBSCRIPTION_BACKGROUND_REFRESH_MS)
        self.subscription_refresh_timer.timeout.connect(self._check_subscription_refresh)

        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self.search_timer.timeout.connect(self._emit_search)

        self.menu_action = QAction(QIcon(ICON_PATH), PLUGIN_NAME, self.iface.mainWindow())
        self.menu_action.setCheckable(True)
        self.menu_action.setChecked(True)
        self.menu_action.toggled.connect(self.toolbar.setVisible)
        self.toolbar.visibilityChanged.connect(self.menu_action.setChecked)
        self.iface.addPluginToMenu("OnGeo", self.menu_action)

    def _show_ongeo_info(self, event=None):
        """Load and show the CMS-managed plugin information."""
        if self.info_panel_task is not None:
            return
        self.info_panel_generation += 1
        generation = self.info_panel_generation
        self.info_panel_task = self._add_task(
            "OnGeo information",
            self._info_panel_task,
            lambda exception, result=None: self._info_panel_finished(
                exception, result, generation
            ),
        )

    @staticmethod
    def _info_panel_task(task):
        """Fetch information-panel HTML outside the UI thread."""
        if task.isCanceled():
            return {"canceled": True}
        try:
            return {"content_html": fetch_info_panel_html(), "error": None}
        except Exception as exc:
            return {"content_html": None, "error": str(exc)}

    def _info_panel_finished(self, exception, result, generation):
        """Open the information dialog after the CMS request completes."""
        if generation != self.info_panel_generation:
            return
        self.info_panel_task = None
        result = result or {}
        content_html = result.get("content_html")
        if exception or result.get("error") or result.get("canceled") or not content_html:
            self._log_warning(exception or result.get("error") or "Information request cancelled.")
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Could not load OnGeo information. Showing default information.",
                level=MESSAGE_WARNING,
                duration=5,
            )
        show_ongeo_info(self.iface.mainWindow(), content_html)

    def _abort_login(self):
        """Stop an incomplete OAuth authorization flow."""
        self.auth_generation += 1
        self.auth_client.cancel_login()
        if self.auth_callback_timer is not None:
            self.auth_callback_timer.stop()
        self._update_auth_ui()

    def _on_account_clicked(self):
        """Start or end the OnGeo user session."""
        if self.auth_client.is_authenticated():
            self._logout_user()
            return

        if self.auth_client.login_in_progress():
            self._abort_login()
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Sign in cancelled.",
                level=MESSAGE_INFO,
                duration=3,
            )
            return

        self._start_login()

    def _start_login(self):
        """Start the OAuth authorization code flow with PKCE."""
        self.auth_refresh_warning_shown = False
        self.auth_generation += 1
        try:
            login_url = self.auth_client.start_login()
        except Exception as exc:
            self._log_warning(exc)
            self.iface.messageBar().pushMessage(
                "OnGeo",
                str(exc),
                level=MESSAGE_WARNING,
                duration=5,
            )
            self._update_auth_ui()
            return

        self._update_auth_ui()
        if self.auth_callback_timer is not None:
            self.auth_callback_timer.start()

        if not QDesktopServices.openUrl(QUrl(login_url)):
            self._abort_login()
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Could not open the system browser.",
                level=MESSAGE_WARNING,
                duration=5,
            )
            return

    def _check_auth_callback(self):
        """Handle a callback received by the local OAuth listener."""
        if self.auth_client.login_timed_out():
            self._abort_login()
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Sign in timed out. Try again.",
                level=MESSAGE_WARNING,
                duration=5,
            )
            return

        result = self.auth_client.consume_callback_result()
        if result is None:
            return

        if self.auth_callback_timer is not None:
            self.auth_callback_timer.stop()

        self._activate_qgis_window()

        if result.get("error"):
            description = result.get("error_description") or result.get("error")
            self._abort_login()
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Sign in failed: {}".format(description),
                level=MESSAGE_WARNING,
                duration=5,
            )
            return

        code = result.get("code")
        if not code:
            self._abort_login()
            self._log_warning("OAuth callback did not contain authorization code.")
            return

        self.account_button.setText("")
        self.account_button.setToolTip("Signing in to OnGeo...")
        self.account_button.setEnabled(False)
        generation = self.auth_generation
        self.auth_exchange_task = self._add_task(
            "Checking OnGeo access",
            self._auth_token_task,
            lambda exception, result=None: self._auth_exchange_finished(
                exception, result, generation
            ),
            auth_client=self.auth_client,
            authorization_code=code,
            flags=_quiet_task_flags(),
        )

    def _activate_qgis_window(self):
        """Try to activate the QGIS main window after sign in."""
        try:
            window = self.iface.mainWindow()
            if window is None:
                return
            window.raise_()
            window.activateWindow()
        except Exception as exc:
            self._log_warning("Could not activate QGIS window: {}".format(exc))

    @staticmethod
    def _auth_token_task(task, auth_client, authorization_code=None):
        """Fetch login or refresh tokens outside the UI thread."""
        if task.isCanceled():
            return {"canceled": True}
        try:
            if authorization_code is None:
                token_data = auth_client.refresh_access_token()
            else:
                token_data = auth_client.exchange_code_for_tokens(authorization_code)
            return {"token_data": token_data, "error": None}
        except AuthRejectedError as exc:
            return {"error": str(exc), "rejected": True}
        except AuthTransientError as exc:
            return {"error": str(exc), "transient": True}
        except Exception as exc:
            return {"error": str(exc), "transient": True}

    def _auth_exchange_finished(self, exception, result, generation):
        """Complete login after the token endpoint responds."""
        if generation != self.auth_generation:
            return
        self.auth_exchange_task = None
        result = result or {}

        if exception or result.get("error") or result.get("canceled"):
            self._logout_user(show_message=False)
            message = exception or result.get("error") or "Sign in cancelled."
            self._log_warning(message)
            self.iface.messageBar().pushMessage(
                "OnGeo", "Sign in failed. Please try again.", level=MESSAGE_WARNING, duration=5
            )
            return

        try:
            self.auth_client.apply_token_response(result.get("token_data"))
            self.auth_refresh_warning_shown = False
        except Exception as exc:
            self._logout_user(show_message=False)
            self._log_warning(exc)
            self.iface.messageBar().pushMessage(
                "OnGeo", "Sign in failed. Please try again.", level=MESSAGE_WARNING, duration=5
            )
            return

        has_subscription = self.auth_client.has_subscriber_role()
        if not has_subscription:
            self._clear_auth_sensitive_ui()
            self._start_subscription_refresh()
            self._show_subscription_required()
        else:
            self._stop_subscription_refresh()
            self._close_subscription_dialog()
        self._update_auth_ui()
        if not has_subscription:
            return
        self.iface.messageBar().pushMessage(
            "OnGeo",
            "Signed in successfully.",
            level=MESSAGE_INFO,
            duration=3,
        )

    def _check_auth_refresh(self):
        """Refresh the access token before it expires."""
        if not self.auth_client.is_authenticated():
            return
        if self.auth_refresh_task is not None:
            return
        if not self.auth_client.access_token_expired(skew_seconds=60):
            return
        if not self.auth_client.refresh_token():
            self._expire_session()
            return

        self._start_auth_refresh_task("Refreshing OnGeo access")

    def _start_auth_refresh_task(self, description):
        """Run one token refresh task for the current authentication generation."""
        generation = self.auth_generation
        self.auth_refresh_task = self._add_task(
            description,
            self._auth_token_task,
            lambda exception, result=None: self._auth_refresh_finished(
                exception, result, generation
            ),
            auth_client=self.auth_client,
            flags=_quiet_task_flags(),
        )

    def _show_auth_refresh_warning(self):
        """Report a transient refresh failure once until a later success."""
        if self.auth_refresh_warning_shown:
            return
        self.auth_refresh_warning_shown = True
        self.iface.messageBar().pushMessage(
            "OnGeo",
            "Could not refresh your session. It will retry automatically.",
            level=MESSAGE_WARNING,
            duration=5,
        )

    def _complete_auth_retry_callbacks(self, succeeded):
        """Resume or finish requests that received one authentication failure."""
        callbacks = self._auth_retry_callbacks
        self._auth_retry_callbacks = []
        for generation, retry, on_failure in callbacks:
            if generation != self.auth_generation:
                continue
            if succeeded:
                retry()
            elif on_failure is not None:
                on_failure()

    def _refresh_after_401(self, retry, on_failure=None):
        """Refresh once, then resume a request that received HTTP 401."""
        if not self._auth_features_enabled():
            if on_failure is not None:
                on_failure()
            return
        self._auth_retry_callbacks.append((self.auth_generation, retry, on_failure))
        if self.auth_refresh_task is not None:
            return
        if not self.auth_client.refresh_token():
            self._auth_retry_callbacks = []
            self._expire_session()
            return
        self._start_auth_refresh_task("Refreshing OnGeo access")

    def _auth_refresh_finished(self, exception, result, generation):
        """Apply refreshed tokens or finish requests waiting for a refresh."""
        if generation != self.auth_generation:
            return
        self.auth_refresh_task = None
        result = result or {}
        if exception or result.get("error") or result.get("canceled"):
            message = exception or result.get("error") or "Token refresh cancelled."
            self._log_warning(message)
            if result.get("rejected"):
                self._auth_retry_callbacks = []
                self._expire_session()
            else:
                self._show_auth_refresh_warning()
                self._complete_auth_retry_callbacks(False)
            return

        had_subscription = self.auth_client.has_subscriber_role()
        try:
            self.auth_client.apply_token_response(result.get("token_data"))
            self.auth_refresh_warning_shown = False
        except Exception as exc:
            self._log_warning(exc)
            self._show_auth_refresh_warning()
            self._complete_auth_retry_callbacks(False)
            return

        has_subscription = self.auth_client.has_subscriber_role()
        if not has_subscription:
            self._clear_auth_sensitive_ui()
            self._start_subscription_refresh()
        else:
            self._stop_subscription_refresh()
            self._close_subscription_dialog()
        self._update_auth_ui()
        if had_subscription and not has_subscription:
            self._show_subscription_required()
        self._complete_auth_retry_callbacks(has_subscription)

    def _show_subscription_required(self):
        """Display the subscription prompt once while access is unavailable."""
        self._start_subscription_refresh(SUBSCRIPTION_DIALOG_REFRESH_MS)
        dialog = self.subscription_dialog
        if dialog is not None and dialog.isVisible():
            return
        dialog = show_subscription_required(self.iface.mainWindow())
        dialog.finished.connect(self._on_subscription_dialog_finished)
        self.subscription_dialog = dialog

    def _on_subscription_dialog_finished(self, _result):
        """Reduce subscription polling after the user closes the prompt."""
        closed_by_user = self.subscription_dialog is not None
        self.subscription_dialog = None
        if (
            closed_by_user
            and self.auth_client.is_authenticated()
            and not self.auth_client.has_subscriber_role()
        ):
            self._start_subscription_refresh(SUBSCRIPTION_BACKGROUND_REFRESH_MS)

    def _close_subscription_dialog(self):
        """Close the subscription prompt after access becomes available."""
        dialog = self.subscription_dialog
        self.subscription_dialog = None
        if dialog is not None:
            dialog.close()

    def _start_subscription_refresh(self, interval_ms=SUBSCRIPTION_BACKGROUND_REFRESH_MS):
        """Poll the current session while a subscription is unavailable."""
        if self.subscription_refresh_timer is not None:
            if self.subscription_refresh_timer.interval() != interval_ms:
                self.subscription_refresh_timer.setInterval(interval_ms)
            self.subscription_refresh_timer.start()

    def _stop_subscription_refresh(self):
        """Stop subscription polling when it is no longer needed."""
        if self.subscription_refresh_timer is not None:
            self.subscription_refresh_timer.stop()

    def _check_subscription_refresh(self):
        """Refresh access until a subscription is detected."""
        if (
            not self.auth_client.is_authenticated()
            or self.auth_client.has_subscriber_role()
            or self.auth_refresh_task is not None
        ):
            self._stop_subscription_refresh()
            return
        if not self.auth_client.refresh_token():
            return

        self._start_auth_refresh_task("Checking OnGeo subscription")

    def _expire_session(self):
        """Clear invalid credentials while retaining selected parcel work."""
        self._logout_user(show_message=False, remove_selected_layers=False)
        self.iface.messageBar().pushMessage(
            "OnGeo",
            "Your session expired. Please sign in again.",
            level=MESSAGE_WARNING,
            duration=5,
        )

    @staticmethod
    def _cancel_task(task):
        """Cancel a QGIS task when it is still available."""
        if task is None:
            return
        try:
            task.cancel()
        except (AttributeError, RuntimeError):
            pass

    def _logout_user(self, show_message=True, remove_selected_layers=True):
        """Sign out locally and disable protected features."""
        self.auth_generation += 1
        if self.auth_callback_timer is not None:
            self.auth_callback_timer.stop()
        self._cancel_task(self.auth_refresh_task)
        self._cancel_task(self.auth_exchange_task)
        self.auth_refresh_task = None
        self.auth_exchange_task = None
        self._auth_retry_callbacks = []
        self._stop_subscription_refresh()
        self._close_subscription_dialog()
        self.auth_client.logout()
        self.auth_refresh_warning_shown = False
        self._clear_auth_sensitive_ui(remove_selected_layers=remove_selected_layers)
        self._update_auth_ui()
        if show_message:
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Signed out.",
                level=MESSAGE_INFO,
                duration=3,
            )

    def _clear_auth_sensitive_ui(self, remove_selected_layers=True):
        """Clear session UI and remove protected map tiles."""
        if self.search_timer is not None:
            self.search_timer.stop()
        for task in (self.search_task, self.add_task, self.identify_task):
            self._cancel_task(task)
        self.search_generation += 1
        self.add_generation += 1
        self._add_in_progress = False
        self.current_query = ""
        self.add_first_generation = None
        self.search_cache.clear()
        self.search_cache_order.clear()
        self.search_cooldown_until = 0.0
        self.search_task = None
        self.add_task = None
        self.identify_task = None
        self.identify_generation += 1
        self._deactivate_parcel_identify_tool()
        if self.search_edit is not None:
            old = self.search_edit.blockSignals(True)
            try:
                self.search_edit.clear()
            finally:
                self.search_edit.blockSignals(old)
        if self.results_popup is not None:
            self.results_popup.hide()
        self._set_results([])
        project = QgsProject.instance()
        if remove_selected_layers:
            self._remove_parcel_layers(project)
        removed_tiles = self._remove_parcel_tile_layers(project)
        self._sync_label_edge_policy()
        self._set_parcel_layer_button_state(False)
        canvas = self.iface.mapCanvas()
        if removed_tiles:
            canvas.redrawAllLayers()
        else:
            canvas.refresh()

    def _auth_features_enabled(self):
        """Return whether protected features are available."""
        return (
            self.auth_client.is_authenticated()
            and self.auth_client.has_subscriber_role()
        )

    def _set_protected_controls_enabled(self, enabled, reason=None):
        """Enable or disable controls that require sign in."""
        reason = reason or "Sign in to use OnGeo parcel tools"
        if self.search_edit is not None:
            self.search_edit.setVisible(True)
            self.search_edit.setEnabled(enabled)
            signed_in = self.auth_client.is_authenticated()
            has_subscription = self.auth_client.has_subscriber_role()
            self.search_edit.setPlaceholderText(
                "Search USA parcel or address"
                if enabled or (signed_in and has_subscription)
                else (
                    "Subscription required to search USA parcels"
                    if signed_in
                    else "Sign in to search USA parcels"
                )
            )
            self.search_edit.setToolTip("" if enabled else reason)
        if self.parcel_layer_button is not None:
            self.parcel_layer_button.setVisible(True)
            self.parcel_layer_button.setEnabled(enabled)
            tooltip = self._parcel_layer_tooltip()
            self.parcel_layer_button.setToolTip(tooltip if enabled else reason)
        self._sync_identify_button_state()

    def _update_auth_ui(self):
        """Update controls to match the OAuth session state."""
        if self.account_button is None:
            return

        logged_in = self.auth_client.is_authenticated()
        has_subscription = self.auth_client.has_subscriber_role()
        login_pending = self.auth_client.login_in_progress()
        self._set_protected_controls_enabled(
            logged_in and has_subscription,
            reason=(
                "An active OnGeo subscription is required"
                if logged_in and not has_subscription
                else "Sign in to use OnGeo parcel tools"
            ),
        )

        self.account_button.setEnabled(True)
        if logged_in:
            self.account_button.setText("")
            self.account_button.setIcon(_stateful_tool_icon(LOGOUT_ICON_BASE_PATH))
            self.account_button.setChecked(True)
            self.account_button.setToolTip("Sign out of OnGeo")
        elif login_pending:
            self.account_button.setText("")
            self.account_button.setIcon(_stateful_tool_icon(LOGIN_ICON_BASE_PATH))
            self.account_button.setChecked(False)
            self.account_button.setToolTip("Cancel OnGeo sign in")
        else:
            self.account_button.setText("")
            self.account_button.setIcon(_stateful_tool_icon(LOGIN_ICON_BASE_PATH))
            self.account_button.setChecked(False)
            self.account_button.setToolTip("Sign in to OnGeo")
        self._update_user_identity()

    def _update_user_identity(self):
        """Update the reserved user area without changing the toolbar width."""
        if self.user_name_label is None or self.user_status_icon is None:
            return
        name = (
            self.auth_client.user_display_name()
            if self.auth_client.is_authenticated()
            else ""
        )
        if not name:
            self.user_name_label.clear()
            self.user_name_label.setToolTip("")
            self.user_status_icon.hide()
            return
        elide_right = getattr(getattr(Qt, "TextElideMode", Qt), "ElideRight")
        shortened = self.user_name_label.fontMetrics().elidedText(
            name, elide_right, USER_NAME_MAX_WIDTH - 2
        )
        self.user_name_label.setText(shortened)
        self.user_name_label.setToolTip(name)
        self.user_status_icon.show()

    def _show_result_info(self, result):
        """Show details for a selected parcel."""
        show_result_info(self.iface.mainWindow(), result)

    def _parcel_tile_layer_is_visible(self):
        """Return whether an OnGeo parcel tile layer is visible in the project."""
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        for layer in self._parcel_tile_layers(project):
            node = root.findLayer(layer.id())
            if node is not None and node.isVisible():
                return True
        return False

    def _parcel_tile_source_is_visible(self):
        """Return whether the map is at the first available parcel tile zoom."""
        try:
            map_settings = self.iface.mapCanvas().mapSettings()
            extent = map_settings.visibleExtent()
            output_width = map_settings.outputSize().width()
            source_crs = map_settings.destinationCrs()
            tile_crs = QgsCoordinateReferenceSystem("EPSG:3857")
            if source_crs != tile_crs:
                extent = QgsCoordinateTransform(
                    source_crs, tile_crs, QgsProject.instance()
                ).transformBoundingBox(extent)
            resolution = extent.width() / output_width
            if resolution <= 0:
                return False
            zoom = log2(
                WEB_MERCATOR_WORLD_WIDTH / (WEB_MERCATOR_TILE_SIZE * resolution)
            )
            return zoom >= PARCEL_TILE_SOURCE_MIN_ZOOM
        except (
            QgsCsException,
            AttributeError,
            RuntimeError,
            TypeError,
            ValueError,
            ZeroDivisionError,
        ):
            # Some project CRS/extents cannot be reprojected to EPSG:3857.
            # Fall back to the QGIS map scale instead of interrupting map signals.
            return float(self.iface.mapCanvas().scale()) <= PARCEL_TILE_MIN_SCALE

    def _sync_parcel_tile_scale_message(self, *args):
        """Show zoom guidance while a visible parcel layer is outside its scale range."""
        show_message = False
        try:
            show_message = (
                self._auth_features_enabled()
                and self._parcel_tile_layer_is_visible()
                and not self._parcel_tile_source_is_visible()
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        if show_message and not self._parcel_tile_scale_message_visible:
            message_bar = self.iface.messageBar()
            message = message_bar.createMessage("OnGeo", "Zoom in to see parcels.")
            self._parcel_tile_scale_message = message_bar.pushWidget(
                message,
                level=MESSAGE_INFO,
                duration=0,
            )
            self._parcel_tile_scale_message_visible = True
        elif not show_message and self._parcel_tile_scale_message_visible:
            try:
                self.iface.messageBar().popWidget(self._parcel_tile_scale_message)
            except (AttributeError, RuntimeError, TypeError):
                pass
            self._parcel_tile_scale_message = None
            self._parcel_tile_scale_message_visible = False

    def _identify_tooltip(self, enabled=None):
        """Return the tooltip matching the parcel-identification state."""
        if enabled is None and self.identify_button is not None:
            enabled = self.identify_button.isChecked()
        return (
            "Disable parcel identification"
            if enabled
            else "Enable parcel identification"
        )

    def _set_parcel_identify_message_visible(self, visible):
        """Show guidance while the parcel-identification map tool is active."""
        if visible and not self._parcel_identify_message_visible:
            message_bar = self.iface.messageBar()
            message = message_bar.createMessage(
                "OnGeo", "Click a parcel to view its details"
            )
            self._parcel_identify_message = message_bar.pushWidget(
                message,
                level=MESSAGE_INFO,
                duration=0,
            )
            self._parcel_identify_message_visible = True
        elif not visible and self._parcel_identify_message_visible:
            try:
                self.iface.messageBar().popWidget(self._parcel_identify_message)
            except (AttributeError, RuntimeError, TypeError):
                pass
            self._parcel_identify_message = None
            self._parcel_identify_message_visible = False

    def _sync_identify_button_state(self, *args):
        """Enable identification only when the nationwide MVT layer is visible."""
        if self.identify_button is None:
            return
        self.identify_button.setVisible(True)
        if not self._auth_features_enabled():
            self._deactivate_parcel_identify_tool()
            self.identify_button.setEnabled(False)
            self.identify_button.setToolTip(
                "An active OnGeo subscription is required"
                if self.auth_client.is_authenticated()
                else "Sign in to identify OnGeo parcels"
            )
            return
        if self._add_in_progress:
            self._deactivate_parcel_identify_tool()
            self.identify_button.setEnabled(False)
            self.identify_button.setToolTip("Adding selected parcel...")
            return
        if self._parcel_tile_layer_is_visible():
            self.identify_button.setEnabled(True)
            self.identify_button.setToolTip(self._identify_tooltip())
            return
        # Selected search-result layers do not enable parcel identification.
        # Invalidate any in-flight lookup before switching the map tool off.
        if self.identify_task is not None:
            self._cancel_task(self.identify_task)
            self.identify_task = None
            self.identify_generation += 1
        self._deactivate_parcel_identify_tool()
        self.identify_button.setEnabled(False)
        self.identify_button.setToolTip(
            "Show the OnGeo USA Parcel Layer to identify parcels"
        )

    def _ensure_parcel_identify_tool(self):
        """Create the reusable map tool for parcel information."""
        if self.parcel_identify_tool is None:
            self.parcel_identify_tool = ParcelIdentifyTool(self.iface.mapCanvas())
            self.parcel_identify_tool.mapPointClicked.connect(self._on_parcel_identified)
        return self.parcel_identify_tool

    def _on_identify_button_clicked(self, checked):
        """Activate or deactivate parcel identification on the map canvas."""
        if not self._auth_features_enabled():
            self._sync_identify_button_state()
            return
        canvas = self.iface.mapCanvas()
        tool = self._ensure_parcel_identify_tool()
        if checked:
            if not self._parcel_tile_layer_is_visible():
                self._sync_identify_button_state()
                return
            self._previous_map_tool = canvas.mapTool()
            canvas.setMapTool(tool)
            self._set_identify_button_checked(True)
        else:
            if canvas.mapTool() is tool:
                self._restore_previous_map_tool()
            self._set_identify_button_checked(False)

    def _on_map_tool_set(self, new_tool, _old_tool=None):
        """Keep the identify button aligned with the active QGIS map tool."""
        enabled = new_tool is self.parcel_identify_tool
        if not enabled:
            self._previous_map_tool = None
        self._set_identify_button_checked(enabled)

    def _set_identify_button_checked(self, checked):
        """Set the identify button state without triggering map-tool changes."""
        if self.identify_button is None:
            return
        old = self.identify_button.blockSignals(True)
        try:
            self.identify_button.setChecked(checked)
            self.identify_button.setIcon(
                _stateful_tool_icon(
                    IDENTIFY_ACTIVE_ICON_BASE_PATH
                    if checked
                    else IDENTIFY_INACTIVE_ICON_BASE_PATH,
                    active=checked,
                )
            )
        finally:
            self.identify_button.blockSignals(old)
        if self.identify_button.isEnabled():
            self.identify_button.setToolTip(self._identify_tooltip(checked))
        self._set_parcel_identify_message_visible(checked)

    def _restore_previous_map_tool(self):
        """Return to the map tool that was active before parcel identification."""
        canvas = self.iface.mapCanvas()
        previous = self._previous_map_tool
        self._previous_map_tool = None
        if previous is not None and previous is not self.parcel_identify_tool:
            canvas.setMapTool(previous)
        elif canvas.mapTool() is self.parcel_identify_tool:
            canvas.unsetMapTool(self.parcel_identify_tool)

    def _deactivate_parcel_identify_tool(self):
        """Leave parcel-identification mode and clear its toolbar state."""
        tool = self.parcel_identify_tool
        canvas = self.iface.mapCanvas()
        if tool is not None and canvas.mapTool() is tool:
            self._restore_previous_map_tool()
        else:
            self._previous_map_tool = None
        self._set_identify_button_checked(False)

    def _on_parcel_identified(self, point):
        """Look up the clicked parcel and show the standard information dialog."""
        if (
            not self._auth_features_enabled()
            or self._add_in_progress
            or not self._parcel_tile_layer_is_visible()
        ):
            return
        try:
            source_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            if source_crs != target_crs:
                point = QgsCoordinateTransform(
                    source_crs, target_crs, QgsProject.instance()
                ).transform(point)
            latitude, longitude = float(point.y()), float(point.x())
        except Exception as exc:
            self._log_warning(exc)
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Could not determine the clicked map location.",
                level=MESSAGE_WARNING,
                duration=5,
            )
            return

        self._cancel_task(self.identify_task)
        self.identify_generation += 1
        self._start_locate_parcel(latitude, longitude, self.identify_generation)

    def _start_locate_parcel(
        self, latitude, longitude, identify_generation, auth_retry_attempted=False
    ):
        """Fetch a parcel at a known map location for the current session."""
        if (
            not self._auth_features_enabled()
            or identify_generation != self.identify_generation
        ):
            return
        auth_generation = self.auth_generation
        self.identify_task = self._add_task(
            "OnGeo locate parcel",
            self._locate_parcel_task,
            lambda exception, result=None: self._locate_parcel_finished(
                exception, result, auth_generation, identify_generation
            ),
            latitude=latitude,
            longitude=longitude,
            access_token=self.auth_client.access_token(),
            auth_retry_attempted=auth_retry_attempted,
        )

    @staticmethod
    def _locate_parcel_task(
        task, latitude, longitude, access_token=None, auth_retry_attempted=False
    ):
        """Fetch parcel information for a map location in a background task."""
        common = {
            "latitude": latitude,
            "longitude": longitude,
            "auth_retry_attempted": auth_retry_attempted,
        }
        if task.isCanceled():
            return {"parcel": None, "canceled": True, **common}
        try:
            parcel = locate_parcel(latitude, longitude, access_token=access_token)
            return {"parcel": parcel, "error": None, **common}
        except Exception as exc:
            return _task_error_result(exc, parcel=None, **common)

    def _locate_parcel_finished(
        self, exception, result, auth_generation, identify_generation
    ):
        """Display a located parcel if its request still belongs to this session."""
        if (
            auth_generation != self.auth_generation
            or identify_generation != self.identify_generation
        ):
            return
        self.identify_task = None
        result = result or {}
        if result.get("status_code") == 401:
            if result.get("auth_retry_attempted"):
                self._expire_session()
            else:
                self._refresh_after_401(
                    lambda: self._start_locate_parcel(
                        result.get("latitude"),
                        result.get("longitude"),
                        identify_generation,
                        auth_retry_attempted=True,
                    )
                )
            return
        if (
            not self._auth_features_enabled()
            or not self._parcel_tile_layer_is_visible()
            or result.get("canceled")
        ):
            return
        if exception or result.get("error"):
            self._report_task_error(exception, result, "Parcel identification failed.")
            return
        parcel = result.get("parcel")
        if not isinstance(parcel, dict):
            self._report_task_error(None, result, "Empty parcel location response.")
            return
        self._start_add_result(parcel, zoom_to_parcel=False)
        self._show_result_info(parcel)

    def unload(self):
        """Release toolbar and session resources when disabling the plugin."""
        self.info_panel_generation += 1
        if self.info_panel_task is not None:
            self._cancel_task(self.info_panel_task)
            self.info_panel_task = None
        if self._map_tool_signal_connected:
            try:
                self.iface.mapCanvas().mapToolSet.disconnect(self._on_map_tool_set)
            except (RuntimeError, TypeError):
                pass
            self._map_tool_signal_connected = False
        if self._map_scale_signal_connected:
            try:
                self.iface.mapCanvas().scaleChanged.disconnect(
                    self._sync_parcel_tile_scale_message
                )
            except (RuntimeError, TypeError):
                pass
            self._map_scale_signal_connected = False
        if self._map_extent_signal_connected:
            try:
                self.iface.mapCanvas().extentsChanged.disconnect(
                    self._sync_parcel_tile_scale_message
                )
            except (RuntimeError, TypeError):
                pass
            self._map_extent_signal_connected = False
        self._deactivate_parcel_identify_tool()
        if self._label_order_signals_connected:
            root = QgsProject.instance().layerTreeRoot()
            try:
                root.layerOrderChanged.disconnect(self._sync_selected_parcel_labels)
                root.visibilityChanged.disconnect(self._sync_selected_parcel_labels)
                root.visibilityChanged.disconnect(self._sync_identify_button_state)
                root.visibilityChanged.disconnect(self._sync_parcel_tile_scale_message)
                root.visibilityChanged.disconnect(self._sync_parcel_layer_button_state)
                root.addedChildren.disconnect(self._sync_identify_button_state)
                root.addedChildren.disconnect(self._sync_parcel_layer_button_state)
                root.removedChildren.disconnect(self._sync_identify_button_state)
                root.removedChildren.disconnect(self._sync_parcel_layer_button_state)
            except (RuntimeError, TypeError):
                pass
            self._label_order_signals_connected = False
        if self._label_project_signals_connected:
            project = QgsProject.instance()
            try:
                project.layersAdded.disconnect(self._sync_label_edge_policy)
                project.layerRemoved.disconnect(self._on_project_layer_removed)
                project.layersAdded.disconnect(self._sync_identify_button_state)
            except (RuntimeError, TypeError):
                pass
            self._label_project_signals_connected = False
        for timer in (
            self.auth_callback_timer,
            self.auth_refresh_timer,
            self.subscription_refresh_timer,
        ):
            if timer is not None:
                try:
                    timer.stop()
                except Exception as exc:
                    self._log_warning("Could not stop QGIS timer: {}".format(exc))
        self._logout_user(show_message=False)
        self._remove_network_request_preprocessor()
        if self.menu_action is not None:
            try:
                self.iface.removePluginMenu("OnGeo", self.menu_action)
            except Exception as exc:
                self._log_warning("Could not remove plugin menu: {}".format(exc))
            self.menu_action = None
        if self.results_popup is not None:
            try:
                self.results_popup.hide()
                self.results_popup.deleteLater()
            except Exception as exc:
                self._log_warning("Could not close results popup: {}".format(exc))
            self.results_popup = None
        self._remove_own_toolbar_controls()
        if self.toolbar is not None and self._created_toolbar and not self.toolbar.actions():
            try:
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar.deleteLater()
            except Exception as exc:
                self._log_warning("Could not remove OnGeo toolbar: {}".format(exc))
        self.toolbar = None

    def _get_or_create_toolbar(self):
        """Use the shared OnGeo toolbar, creating it only when absent."""
        main = self.iface.mainWindow()
        for toolbar in main.findChildren(QToolBar):
            if toolbar.objectName() != LEGACY_TOOLBAR_OBJECT_NAME:
                continue
            main.removeToolBar(toolbar)
            toolbar.setParent(None)
            toolbar.deleteLater()

        for toolbar in main.findChildren(QToolBar):
            if toolbar.objectName() != TOOLBAR_OBJECT_NAME:
                continue
            toolbar.setWindowTitle("OnGeo")
            toolbar.setToolTip("OnGeo")
            if toolbar.toggleViewAction():
                toolbar.toggleViewAction().setText("OnGeo")
            return toolbar

        toolbar = self.iface.addToolBar("OnGeo")
        toolbar.setObjectName(TOOLBAR_OBJECT_NAME)
        toolbar.setWindowTitle("OnGeo")
        toolbar.setToolTip("OnGeo")
        toolbar.setMovable(True)
        if toolbar.toggleViewAction():
            toolbar.toggleViewAction().setText("OnGeo")
        self._created_toolbar = True
        return toolbar

    def _add_toolbar_widget(self, widget, name):
        """Add and track an OnGeo control in the shared toolbar."""
        action = self.toolbar.addWidget(widget)
        action.setObjectName("{}{}".format(TOOLBAR_ACTION_PREFIX, name))
        self._toolbar_actions.append(action)
        return action

    def _add_toolbar_separator(self):
        """Add and track an OnGeo separator in the shared toolbar."""
        action = self.toolbar.addSeparator()
        action.setObjectName("{}Separator".format(TOOLBAR_ACTION_PREFIX))
        self._toolbar_actions.append(action)

    def _remove_existing_toolbar_controls(self):
        """Remove controls retained by an earlier OnGeo plugin instance."""
        if self.toolbar is None:
            return
        for action in list(self.toolbar.actions()):
            widget = self.toolbar.widgetForAction(action)
            is_marked = action.objectName().startswith(TOOLBAR_ACTION_PREFIX)
            is_legacy_control = widget is not None and widget.objectName() in {
                "OnGeoLogoLabel",
                SEARCH_INPUT_OBJECT_NAME,
                "OnGeoParcelLayerButton",
                "OnGeoIdentifyButton",
                "OnGeoAccountButton",
                "OnGeoUserIdentity",
            }
            if is_marked or is_legacy_control:
                self.toolbar.removeAction(action)
                action.deleteLater()

    def _remove_own_toolbar_controls(self):
        """Remove this plugin's controls without removing shared OnGeo tools."""
        if self.toolbar is None:
            return
        for action in self._toolbar_actions:
            try:
                self.toolbar.removeAction(action)
                action.deleteLater()
            except RuntimeError:
                pass
        self._toolbar_actions = []

    def _log_warning(self, message):
        """Log a warning without opening a modal dialog."""
        try:
            QgsMessageLog.logMessage(str(message), "OnGeo", MESSAGE_WARNING)
        except Exception:
            # Log outside QGIS when its logger is no longer available.
            logging.getLogger("OnGeo").warning("%s", message)

    def _report_task_error(self, exception, result, fallback_message):
        """Log task errors and show user-facing API errors."""
        result = result or {}
        self._log_warning(exception or result.get("error") or fallback_message)
        user_message = result.get("user_message") or fallback_message
        self.iface.messageBar().pushMessage(
            "OnGeo", user_message, level=MESSAGE_WARNING, duration=5
        )

    def _install_network_request_preprocessor(self):
        """Attach the OAuth token only to OnGeo vector tile requests."""
        if self.network_request_preprocessor_id is not None:
            return
        setter = getattr(QgsNetworkAccessManager, "setRequestPreprocessor", None)
        if setter is None:
            self._log_warning("QGIS network request preprocessor is unavailable.")
            return

        parcel_tile_host = QUrl(
            PARCEL_TILE_MVT_URL_TEMPLATE.format(z=0, x=0, y=0)
        ).host().lower()
        client_header_name = QGIS_CLIENT_HEADER.encode("ascii")
        client_header_value = QGIS_CLIENT_HEADER_VALUE.encode("ascii")

        def preprocess(request):
            try:
                url = request.url()
                host = url.host().lower()
                if host != parcel_tile_host or url.scheme().lower() != "https":
                    return ""
                if url.port(-1) not in (-1, 443):
                    return ""
                if QUrlQuery(url).queryItemValue("client") != QGIS_CLIENT_HEADER_VALUE:
                    return ""
                if not PARCEL_TILE_PATH_PATTERN.search(url.path()):
                    return ""
                if not self._auth_features_enabled():
                    return ""
                request.setRawHeader(client_header_name, client_header_value)
                access_token = self.auth_client.access_token()
                if access_token:
                    request.setRawHeader(
                        b"Authorization",
                        "Bearer {}".format(access_token).encode("utf-8"),
                    )
            except Exception as exc:
                self._log_warning("Could not preprocess OnGeo request: {}".format(exc))
            return ""

        self.network_request_preprocessor = preprocess
        self.network_request_preprocessor_id = setter(preprocess)

    def _remove_network_request_preprocessor(self):
        """Remove the QGIS network request preprocessor."""
        if self.network_request_preprocessor_id is None:
            return
        remover = getattr(QgsNetworkAccessManager, "removeRequestPreprocessor", None)
        if remover is not None:
            try:
                remover(self.network_request_preprocessor_id)
            except Exception as exc:
                self._log_warning("Could not remove request preprocessor: {}".format(exc))
        self.network_request_preprocessor_id = None
        self.network_request_preprocessor = None

    def _on_search_text_changed(self, text):
        """Schedule a debounced parcel search."""
        query = (text or "").strip()
        self.add_first_generation = None
        if query != self.current_query:
            if self.search_task is not None:
                self._cancel_task(self.search_task)
                self.search_task = None
            self.search_generation += 1
            self.current_query = query
            self._set_results([])
            if self.results_popup is not None:
                self.results_popup.hide()
        if len(query) < SEARCH_MIN_CHARS:
            self.search_timer.stop()
            self._set_results([])
            if self.results_popup is not None:
                self.results_popup.hide()
            return
        self.search_timer.start()

    def _emit_search(self):
        """Start a search after the debounce timer expires."""
        self._start_search(self.search_edit.text(), add_first=False)

    def _on_search_return_pressed(self):
        """Add the selected or first search result."""
        query = (self.search_edit.text() or "").strip()
        if not query:
            return
        if len(query) < SEARCH_MIN_CHARS:
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Enter at least {} characters.".format(SEARCH_MIN_CHARS),
                level=MESSAGE_INFO,
                duration=3,
            )
            return
        result = self._result_for_text(query)
        if result is None and self.results:
            result = self.results[0]
        if result is not None:
            self._activate_result(result)
            return
        if self.search_timer is not None:
            self.search_timer.stop()
        self._start_search(query, add_first=True)

    def _add_task(self, description, func, finished, flags=None, **kwargs):
        """Submit a background QGIS task."""
        task_kwargs = {"on_finished": finished, **kwargs}
        if flags is not None:
            task_kwargs["flags"] = flags
        task = QgsTask.fromFunction(description, func, **task_kwargs)
        QgsApplication.taskManager().addTask(task)
        return task

    def _start_search(self, query, add_first=False, auth_retry_attempted=False):
        """Start a parcel search."""
        if not self._auth_features_enabled():
            return
        query = (query or "").strip()
        if len(query) < SEARCH_MIN_CHARS:
            self._set_results([])
            return
        cached_results = self._cached_search_results(query)
        if cached_results is not None:
            self._cancel_task(self.search_task)
            self.search_task = None
            self.current_query = query
            self.search_generation += 1
            self.add_first_generation = None
            self._show_search_results(cached_results, add_first=add_first)
            return
        if not add_first and time.monotonic() < self.search_cooldown_until:
            return
        if query == self.current_query and self.search_task is not None:
            if add_first:
                self.add_first_generation = self.search_generation
            return
        self._cancel_task(self.search_task)
        self.current_query = query
        self.search_generation += 1
        generation = self.search_generation
        self.add_first_generation = generation if add_first else None
        self.search_task = self._add_task(
            "OnGeo US parcel search",
            self._search_task,
            lambda exception, result=None: self._search_finished(
                exception, result, generation
            ),
            query=query,
            generation=generation,
            access_token=self.auth_client.access_token(),
            auth_retry_attempted=auth_retry_attempted,
        )

    @staticmethod
    def _search_task(task, query, generation, access_token=None, auth_retry_attempted=False):
        """Fetch search suggestions in a background task."""
        common = {
            "generation": generation,
            "query": query,
            "auth_retry_attempted": auth_retry_attempted,
        }
        if task.isCanceled():
            return {"results": [], "canceled": True, **common}
        try:
            results = search_parcels(query, access_token=access_token)
            return {"results": results, "error": None, **common}
        except Exception as exc:
            return _task_error_result(exc, results=[], **common)

    def _search_finished(self, exception, result=None, task_generation=None):
        """Handle completed parcel search results."""
        result = result or {}
        if not result:
            if task_generation is None or task_generation == self.search_generation:
                self.search_task = None
            return
        if not self._auth_features_enabled():
            if task_generation is None or task_generation == self.search_generation:
                self.search_task = None
            return
        if result.get("generation") != self.search_generation:
            return
        self.search_task = None
        if result.get("status_code") == 401:
            if result.get("auth_retry_attempted"):
                self._expire_session()
            else:
                query = result.get("query")
                failed_generation = result.get("generation")
                add_first = self.add_first_generation == failed_generation

                def retry():
                    if (
                        self.search_generation == failed_generation
                        and self.current_query == query
                    ):
                        self._start_search(
                            query, add_first=add_first, auth_retry_attempted=True
                        )

                self._refresh_after_401(retry)
            return
        if exception or result.get("error"):
            self._set_results([])
            if result.get("status_code") == 429:
                retry_after = result.get("retry_after_seconds")
                retry_after = (
                    retry_after
                    if isinstance(retry_after, int) and retry_after > 0
                    else SEARCH_RATE_LIMIT_RETRY_SECONDS
                )
                self.search_cooldown_until = time.monotonic() + retry_after
            self._report_task_error(exception, result, "Parcel search failed.")
            return
        if result.get("canceled") or result.get("query") != self.current_query:
            return
        results = list(result.get("results") or [])[:SEARCH_DISPLAY_LIMIT]
        self._store_search_results(result.get("query"), results)
        auto_add = self.add_first_generation == result.get("generation")
        self.add_first_generation = None
        self._show_search_results(results, add_first=auto_add)

    def _ensure_results_popup(self):
        """Create the results popup when first needed."""
        if self.results_popup is None:
            self.results_popup = ParcelResultsPopup(self.iface.mainWindow())
            self.results_popup.setStyleSheet(STYLE)
            self.results_popup.resultActivated.connect(self._on_popup_result)
            self.results_popup.infoRequested.connect(self._show_result_info)
            self.results_popup.set_results(self.results)

        return self.results_popup

    def _show_search_results(self, results, add_first=False):
        """Show search results or add the first result."""
        self._set_results(results)
        if add_first and results:
            self._activate_result(results[0])
        elif results and self.search_edit is not None and self.search_edit.hasFocus():
            self._ensure_results_popup().show_for(self.search_edit)

    def _search_cache_key(self, query):
        """Normalize a search query for the local cache."""
        return str(query or "").strip().lower()

    def _cached_search_results(self, query):
        """Return cached search results, if available."""
        return self.search_cache.get(self._search_cache_key(query))

    def _store_search_results(self, query, results):
        """Store search results in the bounded local cache."""
        key = self._search_cache_key(query)
        if not key:
            return
        self.search_cache[key] = list(results or [])
        if key in self.search_cache_order:
            self.search_cache_order.remove(key)
        self.search_cache_order.append(key)
        while len(self.search_cache_order) > SEARCH_CACHE_LIMIT:
            old_key = self.search_cache_order.pop(0)
            self.search_cache.pop(old_key, None)

    def _set_results(self, results):
        """Update the search results model."""
        self.results = list(results or [])[:SEARCH_DISPLAY_LIMIT]
        if self.results_popup is not None:
            self.results_popup.set_results(self.results)

    def _result_for_text(self, text):
        """Find the result matching the search field text."""
        text = str(text or "").strip()
        for result in self.results:
            if result_compact_text(result) == text:
                return result
        return None

    def _on_popup_result(self, result):
        """Add a result selected from the popup."""
        if result is not None:
            self._activate_result(result)

    def _activate_result(self, result):
        """Select a result and start adding its parcel."""
        self._set_search_text_for_result(result)
        if self.results_popup is not None:
            self.results_popup.hide()
        self._start_add_result(result)

    def _set_search_text_for_result(self, result):
        """Put a compact result label in the search field."""
        if self.search_edit is None:
            return
        old = self.search_edit.blockSignals(True)
        self.search_edit.setText(result_compact_text(result))
        self.search_edit.blockSignals(old)

    def _set_add_busy(self, busy):
        """Disable protected controls while adding a selected parcel."""
        enabled = (not busy) and self._auth_features_enabled()
        reason = "Adding selected parcel..." if busy else None
        self._set_protected_controls_enabled(enabled, reason=reason)

    def _finish_add_request(self):
        """Restore controls after the active parcel-add request ends."""
        self.add_task = None
        self._add_in_progress = False
        self._set_add_busy(False)

    def _start_add_result(self, result, zoom_to_parcel=True, auth_retry_attempted=False):
        """Fetch geometry for a selected or identified parcel."""
        if not self._auth_features_enabled() or self._add_in_progress or not result:
            return
        parcel_id = result.get("parcelCode") or result.get("parcelId")
        if not parcel_id:
            self._log_warning("Selected result has no parcel identifier.")
            return
        self.add_generation += 1
        add_generation = self.add_generation
        self._add_in_progress = True
        self._set_add_busy(True)
        generation = self.auth_generation
        self.add_task = self._add_task(
            "OnGeo add parcel",
            self._add_result_task,
            lambda exception, response=None: self._add_result_finished(
                exception, response, generation, add_generation
            ),
            result=result,
            parcel_id=parcel_id,
            zoom_to_parcel=zoom_to_parcel,
            access_token=self.auth_client.access_token(),
            auth_retry_attempted=auth_retry_attempted,
        )

    @staticmethod
    def _add_result_task(
        task,
        result,
        access_token=None,
        parcel_id=None,
        zoom_to_parcel=True,
        auth_retry_attempted=False,
    ):
        """Fetch parcel details in a background task."""
        common = {
            "result": result,
            "zoom_to_parcel": zoom_to_parcel,
            "auth_retry_attempted": auth_retry_attempted,
        }
        if task.isCanceled():
            return {"details": None, "canceled": True, **common}
        parcel_id = parcel_id or result.get("parcelCode") or result.get("parcelId")
        try:
            details = fetch_parcel_details(parcel_id, access_token=access_token)
            return {"details": details, "error": None, **common}
        except Exception as exc:
            return _task_error_result(exc, details=None, **common)

    def _add_result_finished(self, exception, result, generation, add_generation=None):
        """Add the fetched parcel to the QGIS project."""
        if generation != self.auth_generation:
            return
        if add_generation is not None and add_generation != self.add_generation:
            return
        self.add_task = None
        result = result or {}
        if result.get("status_code") == 401:
            if result.get("auth_retry_attempted"):
                self._finish_add_request()
                self._expire_session()
                return

            request_result = result.get("result")
            zoom_to_parcel = result.get("zoom_to_parcel", True)
            active_generation = self.add_generation

            def retry():
                if (
                    active_generation == self.add_generation
                    and self._auth_features_enabled()
                ):
                    self._add_in_progress = False
                    self._start_add_result(
                        request_result,
                        zoom_to_parcel=zoom_to_parcel,
                        auth_retry_attempted=True,
                    )

            def finish_failed_refresh():
                if active_generation == self.add_generation:
                    self._finish_add_request()

            self._refresh_after_401(retry, finish_failed_refresh)
            return

        self._finish_add_request()
        if not self._auth_features_enabled():
            return
        if exception or result.get("error"):
            self._report_task_error(exception, result, "Empty parcel response.")
            return
        if result.get("canceled"):
            return
        try:
            project = QgsProject.instance()
            target_layer = self._current_parcel_layer(project)
            details = result.get("details")
            layer, geom, source_crs = append_parcel_to_layer(
                project,
                details,
                result.get("result"),
                target_layer,
            )
            self.parcel_layer = layer
            self._sync_selected_parcel_labels()
            if result.get("zoom_to_parcel", True):
                zoom_to_geometry(self.iface.mapCanvas(), geom, source_crs)
        except Exception as exc:
            self._log_warning(exc)
            self.iface.messageBar().pushMessage(
                "OnGeo",
                "Could not add the selected parcel.",
                level=MESSAGE_WARNING,
                duration=5,
            )

    def _current_parcel_layer(self, project):
        """Return the current selected parcel layer, if any."""
        if self.parcel_layer is not None:
            try:
                if self.parcel_layer.id() in project.mapLayers():
                    return self.parcel_layer
            except RuntimeError:
                pass
        self.parcel_layer = next(iter(self._parcel_layers(project)), None)
        return self.parcel_layer

    @staticmethod
    def _matching_project_layers(project, property_name):
        """Find plugin layers by their custom property."""
        layers = []
        for layer in project.mapLayers().values():
            try:
                if layer.customProperty(property_name, False):
                    layers.append(layer)
            except RuntimeError:
                pass
        return layers

    def _parcel_layers(self, project):
        """Return layers containing selected parcels."""
        return self._matching_project_layers(project, PARCEL_LAYER_PROPERTY)

    def _on_project_layer_removed(self, layer_id):
        """Synchronize controls after a layer is removed outside the plugin."""
        self._sync_label_edge_policy()
        self._sync_identify_button_state()
        try:
            removed_tile_layer = (
                self.parcel_tile_layer is not None
                and self.parcel_tile_layer.id() == layer_id
            )
        except RuntimeError:
            removed_tile_layer = True
        if removed_tile_layer:
            self.parcel_tile_layer = None
            self._sync_selected_parcel_labels()
        self._sync_parcel_layer_button_state()
        self._sync_parcel_tile_scale_message()

    def _sync_label_edge_policy(self, *args):
        """Avoid cut-off labels while OnGeo layers are present on the canvas."""
        project = QgsProject.instance()
        active = bool(self._parcel_layers(project) or self._parcel_tile_layers(project))
        if active == (self._partial_labels_before_ongeo is not None):
            return
        canvas = self.iface.mapCanvas()
        settings = canvas.labelingEngineSettings()
        labeling_flags = getattr(Qgis, "LabelingFlag", None)
        flag = (
            labeling_flags.UsePartialCandidates
            if labeling_flags is not None
            else getattr(QgsLabelingEngineSettings, "UsePartialCandidates")
        )
        if active:
            self._partial_labels_before_ongeo = settings.testFlag(flag)
            if not self._partial_labels_before_ongeo:
                return
            settings.setFlag(flag, False)
        else:
            previously_enabled = self._partial_labels_before_ongeo
            self._partial_labels_before_ongeo = None
            if not previously_enabled:
                return
            settings.setFlag(flag, True)
        canvas.setLabelingEngineSettings(settings)

    def _sync_selected_parcel_labels(self, *args):
        """Keep labels behind visible non-OnGeo layers and avoid duplicates."""
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        visible_layer_ids = set()
        visible_layers = []
        for layer in root.layerOrder():
            node = root.findLayer(layer.id())
            if node is not None and node.isVisible():
                visible_layers.append(layer)
                visible_layer_ids.add(layer.id())

        selected_layers = self._parcel_layers(project)
        tile_layers = self._parcel_tile_layers(project)
        ongeo_layer_ids = {
            layer.id() for layer in selected_layers + tile_layers
        }

        def is_covered_by_other_layer(layer):
            """Return whether a visible non-OnGeo layer is above ``layer``."""
            for candidate in visible_layers:
                if candidate.id() == layer.id():
                    return False
                if candidate.id() not in ongeo_layer_ids:
                    return True
            return True

        mvt_labels_visible = False
        for layer in tile_layers:
            enabled = (
                layer.id() in visible_layer_ids
                and not is_covered_by_other_layer(layer)
            )
            self._set_parcel_tile_labels_visible(layer, enabled)
            mvt_labels_visible = mvt_labels_visible or enabled

        for layer in selected_layers:
            try:
                enabled = (
                    layer.id() in visible_layer_ids
                    and not mvt_labels_visible
                    and not is_covered_by_other_layer(layer)
                )
                if layer.labelsEnabled() != enabled:
                    layer.setLabelsEnabled(enabled)
                    layer.triggerRepaint()
            except (AttributeError, RuntimeError):
                pass

    @staticmethod
    def _set_parcel_tile_labels_visible(layer, visible):
        """Set vector tile label visibility in QGIS 3 and QGIS 4."""
        try:
            if hasattr(layer, "labelsEnabled") and hasattr(layer, "setLabelsEnabled"):
                if layer.labelsEnabled() != visible:
                    layer.setLabelsEnabled(visible)
                    layer.triggerRepaint()
                return
            has_labeling = layer.labeling() is not None
            if has_labeling != visible:
                layer.setLabeling(parcel_tile_labeling() if visible else None)
                layer.triggerRepaint()
        except (AttributeError, RuntimeError):
            pass

    def _remove_parcel_layers(self, project):
        """Remove layers containing selected parcels."""
        for layer in self._parcel_layers(project):
            try:
                project.removeMapLayer(layer.id())
            except RuntimeError:
                pass
        self.parcel_layer = None

    def _parcel_tile_layers(self, project):
        """Return the OnGeo vector tile layers."""
        return self._matching_project_layers(project, PARCEL_TILE_LAYER_PROPERTY)

    def _keep_single_parcel_tile_layer(self, project, layers):
        """Keep one vector tile layer and remove duplicates."""
        if not layers:
            return None
        keep = layers[0]
        if self.parcel_tile_layer is not None:
            for layer in layers:
                if layer.id() == self.parcel_tile_layer.id():
                    keep = layer
                    break
        for layer in layers:
            if layer.id() != keep.id():
                try:
                    project.removeMapLayer(layer.id())
                except RuntimeError:
                    pass
        return keep

    def _configure_parcel_tile_layer(self, layer):
        """Configure the vector tile source, style, and labels."""
        source = self._parcel_tile_source()
        if layer.source() != source:
            layer.setDataSource(
                source, layer.name(), layer.providerType(), QgsDataProvider.ProviderOptions()
            )
            if not layer.isValid():
                raise ValueError("Could not update parcel tile layer source.")
        layer.setCustomProperty(PARCEL_TILE_LAYER_PROPERTY, True)
        layer.setRenderer(parcel_tile_renderer())
        layer.setLabeling(parcel_tile_labeling())
        if hasattr(layer, "setLabelsEnabled"):
            layer.setLabelsEnabled(True)
        layer.setScaleBasedVisibility(True)
        layer.setMinimumScale(PARCEL_TILE_MIN_SCALE)
        layer.setMaximumScale(0)

    def _parcel_layer_tooltip(self, checked=None):
        """Return the tooltip matching the parcel-layer button state."""
        if checked is None and self.parcel_layer_button is not None:
            checked = self.parcel_layer_button.isChecked()
        return "Hide US parcels" if checked else "Show US parcels"

    def _sync_parcel_layer_button_state(self, *args):
        """Reflect external layer-tree visibility changes in the MVT button."""
        if self.parcel_layer_button is not None:
            self._set_parcel_layer_button_state(self._parcel_tile_layer_is_visible())

    def _set_parcel_layer_button_state(self, checked):
        """Update the parcel tile button state and icon."""
        if self.parcel_layer_button is None:
            return
        self.parcel_layer_button.setChecked(checked)
        self.parcel_layer_button.setText("")
        self.parcel_layer_button.setIcon(
            _stateful_tool_icon(
                PARCEL_LAYER_HIDE_ICON_BASE_PATH
                if checked
                else PARCEL_LAYER_SHOW_ICON_BASE_PATH,
                active=checked,
            )
        )
        if self.parcel_layer_button.isEnabled():
            self.parcel_layer_button.setToolTip(self._parcel_layer_tooltip(checked))

    def _remove_parcel_tile_layers(self, project):
        """Remove OnGeo vector tile layers from the project."""
        layers = self._parcel_tile_layers(project)
        if layers:
            self.iface.mapCanvas().stopRendering()
        removed = False
        for layer in layers:
            try:
                project.removeMapLayer(layer.id())
                removed = True
            except RuntimeError:
                pass
        self.parcel_tile_layer = None
        self._sync_selected_parcel_labels(project)
        self._sync_identify_button_state()
        self._sync_parcel_tile_scale_message()
        return removed

    def _on_parcel_layer_button_clicked(self):
        """Handle clicks on the vector tile button."""
        if not self._auth_features_enabled():
            self._set_parcel_layer_button_state(False)
            return
        checked = self.parcel_layer_button.isChecked() if self.parcel_layer_button is not None else False
        self._toggle_parcel_tile_layer(checked)

    def _toggle_parcel_tile_layer(self, checked):
        """Toggle the nationwide parcel vector tile layer."""
        project = QgsProject.instance()
        if not checked:
            self._remove_parcel_tile_layers(project)
            self._set_parcel_layer_button_state(False)
            self.iface.mapCanvas().redrawAllLayers()
            return

        existing_layer = self._keep_single_parcel_tile_layer(project, self._parcel_tile_layers(project))
        if existing_layer is not None:
            self._configure_parcel_tile_layer(existing_layer)
            place_layer_at_top(project, existing_layer)
            # A layer hidden directly in the QGIS panel must be shown again.
            node = project.layerTreeRoot().findLayer(existing_layer.id())
            if node is not None:
                node.setItemVisibilityChecked(True)
            existing_layer.triggerRepaint()
            self.parcel_tile_layer = existing_layer
            self._sync_selected_parcel_labels(project)
            self._set_parcel_layer_button_state(True)
            self.iface.mapCanvas().refresh()
            self._sync_parcel_tile_scale_message()
            return

        source = self._parcel_tile_source()
        layer = QgsVectorTileLayer(source, PARCEL_TILE_QGIS_LAYER_NAME)
        if not layer.isValid():
            self._set_parcel_layer_button_state(False)
            self._log_warning("Could not load parcel tile layer.")
            return
        try:
            self._configure_parcel_tile_layer(layer)
        except Exception as exc:
            self._set_parcel_layer_button_state(False)
            self._log_warning(exc)
            return
        project.addMapLayer(layer, False)
        place_layer_at_top(project, layer)
        layer.triggerRepaint()
        self.iface.mapCanvas().refresh()
        self.parcel_tile_layer = layer
        self._sync_selected_parcel_labels(project)
        self._set_parcel_layer_button_state(True)
        self._sync_parcel_tile_scale_message()

    def _parcel_tile_source(self):
        """Return the vector tile source for the OnGeo host."""
        tile_url = PARCEL_TILE_MVT_URL_TEMPLATE.format(z="{z}", x="{x}", y="{y}")
        return "type=xyz&url={}?client={}&zmin={}&zmax={}".format(
            tile_url,
            QGIS_CLIENT_HEADER_VALUE,
            PARCEL_TILE_SOURCE_MIN_ZOOM,
            PARCEL_TILE_MAX_ZOOM,
        )
