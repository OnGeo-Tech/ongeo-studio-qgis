"""Plugin and parcel information dialogs."""

import os
from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from qgis.PyQt.QtCore import QRectF, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .config import REPORT_ORDER_URL
from .layers import (
    alternate_parcel_number,
    parcel_number,
    result_addresses,
    result_location_text,
)
from .qt_compat import (
    SCROLLBAR_ALWAYS_OFF,
    SCROLLBAR_AS_NEEDED,
    TEXT_BROWSER_INTERACTION,
    TEXT_PLAIN,
    TEXT_RICH,
)
from .ui import SCROLLBAR_STYLE


PLUGIN_DIR = os.path.dirname(__file__)
ICON_PATH = os.path.join(PLUGIN_DIR, "icons", "plugin_icon.svg")
LOGO_PATH = os.path.join(PLUGIN_DIR, "icons", "logo.svg")
PLUGIN_NAME = "OnGeo US Parcel Explorer"
ONGEO_HOME_URL = "https://ongeo.us/"
UTM_SOURCE = "qgis"
UTM_MEDIUM = "app"
INFO_PANEL_DIALOG_MIN_WIDTH = 520
INFO_PANEL_CONTENT_WIDTH = 488
INFO_PANEL_MAX_CONTENT_HEIGHT = 480
INFO_PANEL_FALLBACK_HTML = """
<h3>OnGeo US Parcel Explorer</h3>
<p>Explore land parcels across the United States directly in QGIS.</p>
<p><a href="https://ongeo.us/">Visit OnGeo</a> to learn more.</p>
"""
_INFO_PANEL_ALLOWED_TAGS = frozenset({
    "a", "b", "blockquote", "br", "div", "em", "h1", "h2", "h3", "h4", "h5",
    "h6", "i", "li", "ol", "p", "span", "strong", "u", "ul",
})
_INFO_PANEL_VOID_TAGS = frozenset({"br"})
_INFO_PANEL_SUPPRESSED_TAGS = frozenset({"script", "style"})
_INFO_PANEL_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "div", "dl", "dt", "dd", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul",
})
_INFO_PANEL_ALLOWED_ONGEO_DOMAINS = frozenset({"ongeo.com", "ongeo.us", "ongeo.pl"})


def _transparent_color():
    """Return a transparent color in PyQt5 and PyQt6."""
    color_enum = getattr(Qt, "GlobalColor", Qt)
    return getattr(color_enum, "transparent")


def _painter_render_hint(name):
    """Return a QPainter render hint in PyQt5 and PyQt6."""
    render_hint = getattr(QPainter, "RenderHint", QPainter)
    return getattr(render_hint, name)


class _InfoPanelHtmlSanitizer(HTMLParser):
    """Keep only safe, balanced rich-text markup for the information panel."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.open_tags = []
        self.suppressed_depth = 0

    def _append_break(self):
        if not self.parts or self.parts[-1] != "<br>":
            self.parts.append("<br>")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _INFO_PANEL_SUPPRESSED_TAGS:
            self.suppressed_depth += 1
            return
        if self.suppressed_depth:
            return
        if tag not in _INFO_PANEL_ALLOWED_TAGS:
            if tag in _INFO_PANEL_BLOCK_TAGS:
                self._append_break()
            return
        if tag in _INFO_PANEL_VOID_TAGS:
            self.parts.append("<{}>".format(tag))
            return
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href"), None)
            if not _is_allowed_info_panel_url(QUrl(href or "")):
                return
            self.parts.append('<a href="{}">'.format(escape(href, quote=True)))
        else:
            self.parts.append("<{}>".format(tag))
        self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        open_count = len(self.open_tags)
        self.handle_starttag(tag, attrs)
        if tag not in _INFO_PANEL_VOID_TAGS and len(self.open_tags) > open_count:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _INFO_PANEL_SUPPRESSED_TAGS:
            if self.suppressed_depth:
                self.suppressed_depth -= 1
            return
        if self.suppressed_depth:
            return
        if tag not in _INFO_PANEL_ALLOWED_TAGS:
            if tag in _INFO_PANEL_BLOCK_TAGS:
                self._append_break()
            return
        if tag in _INFO_PANEL_VOID_TAGS or tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append("</{}>".format(open_tag))
            if open_tag == tag:
                break

    def handle_data(self, data):
        if not self.suppressed_depth:
            self.parts.append(escape(data))

    def close(self):
        super().close()
        while self.open_tags:
            self.parts.append("</{}>".format(self.open_tags.pop()))


def _sanitize_info_panel_html(content_html):
    """Remove executable and remote-media markup from CMS content."""
    if not isinstance(content_html, str) or not content_html.strip():
        return INFO_PANEL_FALLBACK_HTML
    sanitizer = _InfoPanelHtmlSanitizer()
    try:
        sanitizer.feed(content_html)
        sanitizer.close()
    except ValueError:
        return INFO_PANEL_FALLBACK_HTML
    sanitized = "".join(sanitizer.parts).strip()
    return sanitized or INFO_PANEL_FALLBACK_HTML


def _is_allowed_info_panel_url(url):
    """Allow only standard HTTPS links hosted on an OnGeo domain."""
    if not isinstance(url, QUrl) or not url.isValid():
        return False
    host = url.host().lower().rstrip(".")
    is_ongeo_host = any(
        host == domain or host.endswith("." + domain)
        for domain in _INFO_PANEL_ALLOWED_ONGEO_DOMAINS
    )
    return (
        url.scheme().lower() == "https"
        and url.port(-1) in (-1, 443)
        and is_ongeo_host
    )


def _open_info_panel_url(url):
    """Open an approved CMS link in the system browser."""
    if _is_allowed_info_panel_url(url):
        QDesktopServices.openUrl(url)


INFO_PANEL_CONTENT_STYLE = """
QTextBrowser#OnGeoDialogText {
    border: none;
    background: transparent;
    color: #2f2f2f;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
    font-weight: 400;
}
"""


DIALOG_STYLE = """
QDialog {
    background: #ffffff;
    color: #222222;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
}
QLabel#OnGeoDialogTitle {
    color: #161616;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 18px;
    font-weight: 600;
}
QLabel#OnGeoDialogText {
    color: #2f2f2f;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
    font-weight: 400;
}
QLabel#OnGeoDialogFieldValue {
    color: #303030;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
    font-weight: 400;
}
QLabel#OnGeoDialogSectionLabel {
    color: #161616;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
    font-weight: 600;
    margin-top: 2px;
}
QLabel#OnGeoDialogAddressItem {
    color: #303030;
    background: #f8f8f8;
    border-left: 3px solid #fdc600;
    padding: 5px 8px;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 13px;
    font-weight: 400;
}
QPushButton#OnGeoPrimaryButton {
    min-width: 128px;
    min-height: 28px;
    border: none;
    border-radius: 6px;
    background: #fdc600;
    color: #1f1f1f;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 14px;
}
QPushButton#OnGeoPrimaryButton:hover {
    background: #ffd84d;
}
QLabel#OnGeoDialogLink {
    color: #1f1f1f;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 14px;
    font-weight: 600;
    min-height: 28px;
    padding: 3px 0px;
}
"""


def logo_mark_icon():
    """Return the OnGeo mark icon."""
    icon = QIcon(ICON_PATH)
    return icon if not icon.isNull() else QIcon(LOGO_PATH)


def set_full_logo(label, height):
    """Render the full SVG logo at native screen resolution."""
    renderer = QSvgRenderer(LOGO_PATH)
    logo_size = renderer.defaultSize()
    if not renderer.isValid() or logo_size.height() <= 0:
        label.setText("OnGeo")
        return

    width = max(1, round(height * logo_size.width() / logo_size.height()))
    device_ratio_getter = getattr(label, "devicePixelRatioF", None)
    device_ratio = float(device_ratio_getter()) if device_ratio_getter else 1.0
    device_ratio = max(1.0, device_ratio)
    pixmap = QPixmap(
        max(1, round(width * device_ratio)),
        max(1, round(height * device_ratio)),
    )
    pixmap.setDevicePixelRatio(device_ratio)
    pixmap.fill(_transparent_color())
    painter = QPainter(pixmap)
    painter.setRenderHint(_painter_render_hint("Antialiasing"), True)
    painter.setRenderHint(_painter_render_hint("SmoothPixmapTransform"), True)
    renderer.render(painter, QRectF(0, 0, width, height))
    painter.end()

    label.setText("")
    label.setPixmap(pixmap)
    label.setFixedSize(width, height)


def _new_dialog(parent, title, minimum_width):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setWindowIcon(logo_mark_icon())
    dialog.setMinimumWidth(minimum_width)
    dialog.setStyleSheet(DIALOG_STYLE)
    return dialog


def _exec_dialog(dialog):
    """Run a dialog in QGIS 3 or QGIS 4."""
    run = getattr(dialog, "exec", None)
    if run is None:
        run = getattr(dialog, "exec_")
    return run()


def _add_info_row(layout, row, name, value):
    """Add an inline name/value label to the parcel information grid."""
    if value in (None, ""):
        return False

    # Escape field content before inserting it into rich text.
    label = QLabel("<b>{}</b> {}".format(escape(str(name)), escape(str(value))))
    label.setTextFormat(TEXT_RICH)
    label.setObjectName("OnGeoDialogFieldValue")
    label.setWordWrap(True)
    # Align the rich-text label with the section headings.
    label.setIndent(3)
    # Span both columns to keep names and values together.
    layout.addWidget(label, row, 0, 1, 2)
    return True


def _add_address_list(layout, row, addresses):
    """Add the address section to the parcel information grid."""
    addresses = [address for address in addresses or [] if address]
    if not addresses:
        return row
    title = QLabel("Addresses")
    title.setObjectName("OnGeoDialogSectionLabel")
    layout.addWidget(title, row, 0, 1, 2)
    row += 1
    for address in addresses:
        address_label = QLabel(address)
        address_label.setTextFormat(TEXT_PLAIN)
        address_label.setObjectName("OnGeoDialogAddressItem")
        address_label.setWordWrap(True)
        layout.addWidget(address_label, row, 0, 1, 2)
        row += 1
    return row


def _url_with_utm(url, content, parameters=None):
    """Add OnGeo's QGIS attribution parameters to a URL."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(parameters or {})
    query.update(
        {
            "utm_source": UTM_SOURCE,
            "utm_medium": UTM_MEDIUM,
            "utm_content": content,
        }
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _tool_info_url():
    """Return the tracked URL for the plugin information dialog."""
    return _url_with_utm(ONGEO_HOME_URL, "tool_info")


def _subscription_required_url():
    """Return the fixed subscription page URL for the QGIS plugin."""
    return _url_with_utm("https://ongeo.us", "subscription_required")


def _open_url_and_close(dialog, url):
    """Open an external URL and close the information dialog."""
    QDesktopServices.openUrl(QUrl(url))
    dialog.accept()


def _external_link(dialog, text, url):
    """Build a link that opens in the browser and closes its dialog."""
    link = QLabel(
        '<a style="color: #1f1f1f; text-decoration: underline;" href="{}">{}</a>'.format(
            escape(url, quote=True), escape(text)
        )
    )
    link.setTextFormat(TEXT_RICH)
    link.setTextInteractionFlags(TEXT_BROWSER_INTERACTION)
    link.setObjectName("OnGeoDialogLink")
    link.linkActivated.connect(lambda _href: _open_url_and_close(dialog, url))
    return link


def _info_panel_content(content_html, parent=None):
    """Build an information browser sized to its CMS content."""
    content = QTextBrowser(parent)
    content.setObjectName("OnGeoDialogText")
    content.setOpenLinks(False)
    content.anchorClicked.connect(_open_info_panel_url)
    content.setFixedWidth(INFO_PANEL_CONTENT_WIDTH)
    content.setStyleSheet(INFO_PANEL_CONTENT_STYLE)
    content.setHorizontalScrollBarPolicy(SCROLLBAR_ALWAYS_OFF)
    content.document().setDocumentMargin(0)
    content.setHtml(_sanitize_info_panel_html(content_html))
    content.document().setTextWidth(INFO_PANEL_CONTENT_WIDTH)
    content.ensurePolished()
    height = max(1, int(content.document().documentLayout().documentSize().height()) + 4)
    content.setFixedHeight(min(height, INFO_PANEL_MAX_CONTENT_HEIGHT))
    content.setVerticalScrollBarPolicy(
        SCROLLBAR_ALWAYS_OFF
        if height <= INFO_PANEL_MAX_CONTENT_HEIGHT
        else SCROLLBAR_AS_NEEDED
    )
    scrollbar = content.verticalScrollBar()
    scrollbar.setStyleSheet(SCROLLBAR_STYLE)
    scrollbar.setFixedWidth(12)
    return content


def show_ongeo_info(parent, content_html):
    """Show the CMS-managed information about the plugin."""
    dialog = _new_dialog(parent, PLUGIN_NAME, INFO_PANEL_DIALOG_MIN_WIDTH)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)

    logo = QLabel()
    logo.setObjectName("OnGeoDialogLogo")
    set_full_logo(logo, 42)
    layout.addWidget(logo)

    layout.addWidget(_info_panel_content(content_html, dialog))

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    open_button = QPushButton("Visit OnGeo")
    open_button.setObjectName("OnGeoPrimaryButton")
    open_button.clicked.connect(lambda: _open_url_and_close(dialog, _tool_info_url()))
    buttons.addWidget(open_button)
    layout.addLayout(buttons)

    _exec_dialog(dialog)


def show_subscription_required(parent):
    """Explain that the signed-in user needs an active subscription."""
    dialog = _new_dialog(parent, "Active subscription required", 500)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)

    title = QLabel("Active subscription required")
    title.setObjectName("OnGeoDialogTitle")
    layout.addWidget(title)

    message = QLabel(
        "You need an active OnGeo subscription to use this feature. "
        "Subscribe or renew your subscription to get access."
    )
    message.setObjectName("OnGeoDialogText")
    message.setWordWrap(True)
    layout.addWidget(message)

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    subscribe_button = QPushButton("Subscribe or renew")
    subscribe_button.setObjectName("OnGeoPrimaryButton")
    subscribe_button.clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl(_subscription_required_url()))
    )
    buttons.addWidget(subscribe_button)
    layout.addLayout(buttons)

    dialog.setModal(True)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def _report_order_url(result):
    """Return the report URL for the selected parcel."""
    result = result or {}
    parcel_code = result.get("parcelCode") or result.get("parcelId")
    parameters = {"parcelCode": parcel_code} if parcel_code else None
    return _url_with_utm(REPORT_ORDER_URL, "parcel_info", parameters)


def show_result_info(parent, result):
    """Show basic information about a parcel."""
    result = result or {}
    addresses = result_addresses(result)
    dialog = _new_dialog(parent, "Parcel information", 430)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(7)

    parcel = parcel_number(result)
    alternate = alternate_parcel_number(result)
    location = result_location_text(result)
    title = QLabel(parcel or "Parcel")
    title.setTextFormat(TEXT_PLAIN)
    title.setObjectName("OnGeoDialogTitle")
    layout.addWidget(title)

    info_rows = [
        ("Parcel ID:", parcel),
        ("Alternate Parcel ID:", alternate if alternate != parcel else ""),
        ("Location:", location),
    ]
    content_layout = QGridLayout()
    content_layout.setContentsMargins(0, 0, 0, 0)
    content_layout.setVerticalSpacing(5)
    row = 0
    for name, value in info_rows:
        if _add_info_row(content_layout, row, name, value):
            row += 1
    row = _add_address_list(content_layout, row, addresses)
    if not parcel and not alternate and not location and not addresses:
        text = QLabel("No parcel details available.")
        text.setObjectName("OnGeoDialogText")
        text.setWordWrap(True)
        content_layout.addWidget(text, row, 0, 1, 2)
        row += 1

    details_row = QHBoxLayout()
    details_row.setContentsMargins(0, 0, 0, 0)
    details_row.setSpacing(6)
    details_title = QLabel("Details:")
    details_title.setObjectName("OnGeoDialogSectionLabel")
    details_row.addWidget(details_title)
    report_link = _external_link(
        dialog,
        "Get the Full Property Report",
        _report_order_url(result),
    )
    report_link.setWordWrap(False)
    details_row.addWidget(report_link)
    details_row.addStretch(1)
    content_layout.addLayout(details_row, row, 0, 1, 2)
    layout.addLayout(content_layout)

    _exec_dialog(dialog)
