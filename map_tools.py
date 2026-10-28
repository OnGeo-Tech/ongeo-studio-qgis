"""Map tools used by OnGeo US Parcel Explorer."""

from qgis.PyQt.QtCore import pyqtSignal
from qgis.gui import QgsMapTool

from .qt_compat import CURSOR_CROSS, MOUSE_LEFT

class ParcelIdentifyTool(QgsMapTool):
    """Emit a map position for parcel identification."""

    mapPointClicked = pyqtSignal(object)

    def __init__(self, canvas):
        """Create a click-only map tool for parcel identification."""
        super().__init__(canvas)
        self._press_position = None
        self.setCursor(CURSOR_CROSS)

    def canvasPressEvent(self, event):
        """Remember where a potential click began."""
        self._press_position = event.pos() if event.button() == MOUSE_LEFT else None

    def canvasReleaseEvent(self, event):
        """Emit the clicked point in the canvas destination CRS."""
        if event.button() != MOUSE_LEFT:
            return
        press_position = self._press_position
        self._press_position = None
        if press_position is None or (event.pos() - press_position).manhattanLength() > 4:
            return
        try:
            point = self.toMapCoordinates(event.pos())
        except RuntimeError:
            return
        self.mapPointClicked.emit(point)
