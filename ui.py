"""User interface components for OnGeo US Parcel Explorer."""

import os

from qgis.PyQt.QtCore import QEvent, QSize, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .layers import (
    parcel_number,
    result_addresses,
    result_compact_text,
    result_location_text,
)
from .qt_compat import (
    ALIGN_TOP,
    ATTR_STYLED_BACKGROUND,
    ATTR_TRANSPARENT_FOR_MOUSE,
    CURSOR_POINTING_HAND,
    FOCUS_NO,
    MOUSE_LEFT,
    SCROLLBAR_ALWAYS_OFF,
    SCROLLBAR_AS_NEEDED,
    SCROLL_PER_PIXEL,
    TEXT_PLAIN,
)


RESULTS_POPUP_WIDTH = 680
RESULTS_POPUP_MAX_HEIGHT = 206
RESULTS_POPUP_MIN_HEIGHT = 58
PLUGIN_DIR = os.path.dirname(__file__)
INFO_ICON_PATH = os.path.join(PLUGIN_DIR, "icons", "info_icon.svg")
RESULT_ACTION_COLUMN_WIDTH = 28
RESULT_ACTION_ICON_SIZE = QSize(17, 17)
RESULT_TITLE_HEIGHT = 19
RESULT_ADDRESS_HEIGHT = 17


STYLE = """
QToolBar#OnGeoToolbar {
    spacing: 2px;
}
QLabel#OnGeoLogoLabel {
    min-width: 76px;
    max-width: 76px;
    min-height: 22px;
    max-height: 22px;
    margin: 0px 2px;
    padding: 0px;
    background: transparent;
}
QLineEdit#OnGeoSearchInput {
    min-width: 262px;
    max-width: 482px;
    min-height: 20px;
    max-height: 20px;
    border: 1px solid #d8d8d8;
    border-radius: 4px;
    padding: 1px 8px;
    background: #ffffff;
    color: #222222;
}
QLineEdit#OnGeoSearchInput:focus {
    border: 2px solid #fdc600;
}
QLineEdit#OnGeoSearchInput:disabled {
    border: 1px solid #e0e0e0;
    background: #f1f1f1;
    color: #8a8a8a;
}
QToolButton#OnGeoParcelLayerButton,
QToolButton#OnGeoIdentifyButton,
QToolButton#OnGeoAccountButton {
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    padding: 2px;
    border: 2px solid transparent;
    border-radius: 6px;
    background: #FFC800;
}
/* Hover outlines maintain the current state color. */
QToolButton#OnGeoParcelLayerButton:hover:!disabled:!checked,
QToolButton#OnGeoIdentifyButton:hover:!disabled:!checked,
QToolButton#OnGeoAccountButton:hover:!disabled {
    border: 2px solid #30343B;
}
QToolButton#OnGeoParcelLayerButton:checked:!disabled,
QToolButton#OnGeoIdentifyButton:checked:!disabled {
    background: #30343B;
}
QToolButton#OnGeoParcelLayerButton:checked:hover:!disabled,
QToolButton#OnGeoIdentifyButton:checked:hover:!disabled {
    border: 2px solid #FFC800;
}
/* Account is an action, not an active map tool; it remains yellow. */
QToolButton#OnGeoAccountButton:checked:!disabled {
    background: #FFC800;
}
QToolButton#OnGeoParcelLayerButton:disabled,
QToolButton#OnGeoIdentifyButton:disabled,
QToolButton#OnGeoAccountButton:disabled {
    background: #F2F2F2;
    border: 2px solid transparent;
}
QWidget#OnGeoUserIdentity {
    background: transparent;
}
QLabel#OnGeoUserName {
    background: transparent;
    color: #30343B;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 12px;
    font-weight: 500;
}
QLabel#OnGeoUserStatusIcon {
    background: transparent;
}
QFrame#OnGeoResultsPopup {
    background: #ffffff;
    border: 1px solid #e5e5e5;
    border-radius: 6px;
}
QListWidget#OnGeoResultsList {
    background: #ffffff;
    border: 0px;
    outline: 0px;
    padding: 4px;
}
QListWidget#OnGeoResultsList::item {
    border: 0px;
    padding: 0px;
    margin: 0px;
}
QListWidget#OnGeoResultsList::item:selected {
    background: transparent;
}
QWidget#OnGeoResultItem {
    background: #ffffff;
    border: 0px;
    border-bottom: 1px solid #e8e8e8;
    border-radius: 0px;
}
QWidget#OnGeoResultItem:hover {
    background: #f7f7f7;
}
QLabel#OnGeoResultTitle {
    color: #404040;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 14px;
    font-weight: 400;
}
QLabel#OnGeoResultAddress {
    color: #6e6e6e;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 12px;
    font-weight: 400;
}
"""


SCROLLBAR_STYLE = """
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 6px 2px 6px 0px;
    border: 0px;
}
QScrollBar::handle:vertical {
    background: #b8b8b8;
    border-radius: 6px;
    min-height: 32px;
    border: 0px;
}
QScrollBar::handle:vertical:hover {
    background: #737373;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
    width: 0px;
    border: 0px;
    background: transparent;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
    border: 0px;
}
QScrollBar::up-arrow:vertical,
QScrollBar::down-arrow:vertical {
    width: 0px;
    height: 0px;
    border: 0px;
    background: transparent;
}
"""


INFO_BUTTON_STYLE = """
QToolButton#OnGeoInfoButton {
    min-width: 26px;
    max-width: 26px;
    min-height: 26px;
    max-height: 26px;
    border: none;
    border-radius: 6px;
    background: #fdc600;
    padding: 0px;
    margin: 0px;
}
QToolButton#OnGeoInfoButton:hover {
    background: #ffd633;
    color: #111111;
}
QToolButton#OnGeoInfoButton:pressed {
    background: #e1b000;
    color: #111111;
}
"""


def _result_title_text(result):
    """Build a compact result title without its address list."""
    parcel = parcel_number(result)
    location_parts = [part.strip() for part in result_location_text(result).split(",") if part.strip()]
    location = ", ".join(reversed(location_parts))
    if parcel and location:
        return "{}, {}".format(parcel, location)
    return parcel or location or result_compact_text(result)


class ParcelResultWidget(QWidget):
    """Display one parcel search result."""

    activated = pyqtSignal(object)
    infoRequested = pyqtSignal(object)

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.result = result or {}
        self.info_button = None
        self.setObjectName("OnGeoResultItem")
        self.setCursor(CURSOR_POINTING_HAND)
        self.setAttribute(ATTR_STYLED_BACKGROUND, True)
        self._build_ui()

    def _build_ui(self):
        """Build the search result widget."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)

        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(8)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(2)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(4)
        action_container = QWidget()
        action_container.setFixedWidth(RESULT_ACTION_COLUMN_WIDTH)
        action_container.setLayout(action_row)

        addresses = result_addresses(self.result)
        shown_addresses = addresses[:1]

        title = _result_title_text(self.result)
        title_label = QLabel(title)
        title_label.setTextFormat(TEXT_PLAIN)
        title_label.setObjectName("OnGeoResultTitle")
        title_label.setWordWrap(False)
        title_label.setFixedHeight(RESULT_TITLE_HEIGHT)
        title_label.setAttribute(ATTR_TRANSPARENT_FOR_MOUSE, True)
        text_column.addWidget(title_label)

        if shown_addresses:
            label = QLabel(shown_addresses[0])
            label.setTextFormat(TEXT_PLAIN)
            label.setObjectName("OnGeoResultAddress")
            label.setWordWrap(False)
            label.setFixedHeight(RESULT_ADDRESS_HEIGHT)
            label.setAttribute(ATTR_TRANSPARENT_FOR_MOUSE, True)
            text_column.addWidget(label)

        self.info_button = QToolButton()
        self.info_button.setObjectName("OnGeoInfoButton")
        self.info_button.setIcon(QIcon(INFO_ICON_PATH))
        self.info_button.setIconSize(RESULT_ACTION_ICON_SIZE)
        self.info_button.setToolTip("Parcel information")
        self.info_button.setFocusPolicy(FOCUS_NO)
        self.info_button.setCursor(CURSOR_POINTING_HAND)
        self.info_button.setStyleSheet(INFO_BUTTON_STYLE)
        self.info_button.clicked.connect(self._emit_info)
        action_row.addStretch(1)
        action_row.addWidget(self.info_button)

        content_row.addLayout(text_column, 1)
        content_row.addWidget(action_container, 0, ALIGN_TOP)
        layout.addLayout(content_row)

    def _emit_info(self):
        """Show details for a search result."""
        self.infoRequested.emit(self.result)

    def mouseReleaseEvent(self, event):
        """Select a parcel with a single click."""
        if event.button() == MOUSE_LEFT:
            self.activated.emit(self.result)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ParcelResultsPopup(QFrame):
    """Display parcel search results."""

    resultActivated = pyqtSignal(object)
    infoRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("OnGeoResultsPopup")
        self.setFocusPolicy(FOCUS_NO)
        self._anchor = None
        self.items = []
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(12)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 36))
        self.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.list_widget = QListWidget(self)
        self.list_widget.setObjectName("OnGeoResultsList")
        self.list_widget.setHorizontalScrollBarPolicy(SCROLLBAR_ALWAYS_OFF)
        self.list_widget.setVerticalScrollBarPolicy(SCROLLBAR_AS_NEEDED)
        self.list_widget.setUniformItemSizes(False)
        self.list_widget.setFocusPolicy(FOCUS_NO)
        self.list_widget.setVerticalScrollMode(SCROLL_PER_PIXEL)
        layout.addWidget(self.list_widget)
        self.setMinimumWidth(RESULTS_POPUP_WIDTH)
        self.setMaximumHeight(RESULTS_POPUP_MAX_HEIGHT)
        self.apply_runtime_styles()

    def apply_runtime_styles(self):
        """Apply styles to Qt controls."""
        scrollbar = self.list_widget.verticalScrollBar()
        scrollbar.setStyleSheet(SCROLLBAR_STYLE)
        scrollbar.setFixedWidth(12)

    def set_results(self, results):
        """Set the displayed search results."""
        self.items = []
        self.list_widget.clear()
        for result in list(results or []):
            item = QListWidgetItem()
            widget = ParcelResultWidget(result, self.list_widget)
            widget.activated.connect(self.resultActivated.emit)
            widget.infoRequested.connect(self.infoRequested.emit)
            item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)
            self.items.append((item, widget))

    def _content_height(self):
        """Calculate the result list height."""
        total = 8
        for item, widget in self.items:
            total += max(item.sizeHint().height(), widget.sizeHint().height())
        return min(RESULTS_POPUP_MAX_HEIGHT, max(RESULTS_POPUP_MIN_HEIGHT, total))

    def _resize_to_content(self):
        """Size the search results popup to its current content."""
        if not self.items:
            return
        width = max(self.width(), RESULTS_POPUP_WIDTH)
        height = self._content_height()
        self.resize(width, height)
        self.list_widget.setFixedSize(max(1, width - 2), max(1, height - 2))

    def show_for(self, anchor):
        """Position the popup below the search field."""
        if not self.items:
            self.hide()
            return
        if anchor is not self._anchor:
            if self._anchor is not None:
                self._anchor.removeEventFilter(self)
            self._anchor = anchor
            anchor.installEventFilter(self)
        width = max(anchor.width(), RESULTS_POPUP_WIDTH)
        self.resize(width, self._content_height())
        self._resize_to_content()
        parent = self.parentWidget()
        point = anchor.mapToGlobal(anchor.rect().bottomLeft())
        if parent is not None:
            point = parent.mapFromGlobal(point)
        self.move(point)
        self.show()
        self.raise_()
        anchor.setFocus()

    def eventFilter(self, watched, event):
        """Hide results outside the search field and restore them on refocus."""
        if watched is self._anchor:
            event_type = getattr(QEvent, "Type", QEvent)
            if event.type() == event_type.FocusOut:
                self.hide()
            elif event.type() == event_type.FocusIn and self.items:
                self.show_for(self._anchor)
        return super().eventFilter(watched, event)
