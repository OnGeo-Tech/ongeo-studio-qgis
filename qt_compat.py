"""Qt and QGIS 3/4 compatibility helpers."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAbstractItemView
from qgis.core import Qgis


def _qt_value(enum_name, value_name):
    """Return a Qt enum value across PyQt5 and PyQt6."""
    enum = getattr(Qt, enum_name, None)
    if enum is not None and hasattr(enum, value_name):
        return getattr(enum, value_name)
    return getattr(Qt, value_name)


def _qgis_value(enum_name, value_name):
    """Return a Qgis enum value across QGIS 3 and QGIS 4."""
    enum = getattr(Qgis, enum_name, None)
    if enum is not None and hasattr(enum, value_name):
        return getattr(enum, value_name)
    return getattr(Qgis, value_name)


def _item_view_value(enum_name, value_name):
    """Return a QAbstractItemView enum value across PyQt5 and PyQt6."""
    enum = getattr(QAbstractItemView, enum_name, None)
    if enum is not None and hasattr(enum, value_name):
        return getattr(enum, value_name)
    return getattr(QAbstractItemView, value_name)


def qgs_field_type(value_name):
    """Choose a QgsField type compatible with the QGIS version."""
    if Qgis.versionInt() >= 33800:
        from qgis.PyQt.QtCore import QMetaType

        enum = getattr(QMetaType, "Type", QMetaType)
        value_name = {"String": "QString"}.get(value_name, value_name)
    else:
        from qgis.PyQt.QtCore import QVariant

        enum = getattr(QVariant, "Type", QVariant)
    return getattr(enum, value_name)


ALIGN_TOP = _qt_value("AlignmentFlag", "AlignTop")
ATTR_STYLED_BACKGROUND = _qt_value("WidgetAttribute", "WA_StyledBackground")
ATTR_TRANSPARENT_FOR_MOUSE = _qt_value("WidgetAttribute", "WA_TransparentForMouseEvents")
CURSOR_POINTING_HAND = _qt_value("CursorShape", "PointingHandCursor")
CURSOR_CROSS = _qt_value("CursorShape", "CrossCursor")
FOCUS_NO = _qt_value("FocusPolicy", "NoFocus")
MOUSE_LEFT = _qt_value("MouseButton", "LeftButton")
SCROLLBAR_ALWAYS_OFF = _qt_value("ScrollBarPolicy", "ScrollBarAlwaysOff")
SCROLLBAR_AS_NEEDED = _qt_value("ScrollBarPolicy", "ScrollBarAsNeeded")
SCROLL_PER_PIXEL = _item_view_value("ScrollMode", "ScrollPerPixel")
TEXT_PLAIN = _qt_value("TextFormat", "PlainText")
TEXT_RICH = _qt_value("TextFormat", "RichText")
TEXT_BROWSER_INTERACTION = _qt_value("TextInteractionFlag", "TextBrowserInteraction")

MESSAGE_INFO = _qgis_value("MessageLevel", "Info")
MESSAGE_WARNING = _qgis_value("MessageLevel", "Warning")
