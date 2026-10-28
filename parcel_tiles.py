"""OnGeo US Parcel Explorer vector tile rendering and labeling."""

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsPalLayerSettings,
    QgsVectorTileBasicLabeling,
    QgsVectorTileBasicLabelingStyle,
    QgsVectorTileBasicRenderer,
    QgsWkbTypes,
)

from .config import (
    PARCEL_TILE_LAYER_NAME,
    PARCEL_TILE_MAX_ZOOM,
    PARCEL_TILE_MIN_ZOOM,
    PARCEL_TILE_STROKE_WIDTH,
)
from .layers import (
    configure_parcel_label_position,
    configure_parcel_label_visibility,
    parcel_label_text_format,
)


# A parcel code may carry a stable label anchor in one of two encodings:
# "...xm1117118349.yp333344452" stores WGS 84 lon/lat, while
# "...jivtpz.7hh918" stores absolute Web Mercator coordinates in base 36.
_PARCEL_WGS84_ANCHOR_PATTERN = "[.]x[mp][0-9]+[.]y[mp][0-9]+$"
_PARCEL_WEB_MERCATOR_ANCHOR_PATTERN = "[.][0-9a-z]{6}[.][0-9a-z]{6}$"
_PARCEL_BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"

_PARCEL_WGS84_ANCHOR_POINT = """transform(
    make_point(
        to_real(regexp_substr("parcel_code", '(?<=[.]x[mp])[0-9]+'))
            * if(regexp_match("parcel_code", '[.]xm[0-9]+[.]y'), -1, 1) / 10000000,
        to_real(regexp_substr("parcel_code", '(?<=[.]y[mp])[0-9]+'))
            * if(regexp_match("parcel_code", '[.]ym[0-9]+$'), -1, 1) / 10000000
    ),
    'EPSG:4326', 'EPSG:3857'
)"""


def _base36_value_expression(code_expression):
    """Return a QGIS expression that converts a base-36 string to a number."""
    return """array_sum(array_foreach(
        generate_series(0, length({code}) - 1),
        (strpos('{alphabet}', substr({code}, @element + 1, 1)) - 1)
            * (36 ^ (length({code}) - @element - 1))
    ))""".format(code=code_expression, alphabet=_PARCEL_BASE36_ALPHABET)


_PARCEL_WEB_MERCATOR_X = _base36_value_expression(
    "regexp_substr(\"parcel_code\", '(?<=[.])[0-9a-z]{6}(?=[.][0-9a-z]{6}$)')"
)
_PARCEL_WEB_MERCATOR_Y = _base36_value_expression(
    "regexp_substr(\"parcel_code\", '(?<=[.])[0-9a-z]{6}$')"
)
_PARCEL_WEB_MERCATOR_ANCHOR_POINT = """make_point(
    -({x_coordinate}) / 100,
    ({y_coordinate}) / 100
)""".format(
    x_coordinate=_PARCEL_WEB_MERCATOR_X,
    y_coordinate=_PARCEL_WEB_MERCATOR_Y,
)

_PARCEL_LABEL_POINT_EXPRESSION = """CASE WHEN regexp_match(
    "parcel_code", '{wgs84_pattern}'
) THEN {wgs84_anchor}
WHEN regexp_match("parcel_code", '{web_mercator_pattern}')
THEN {web_mercator_anchor}
ELSE point_on_surface($geometry) END""".format(
    wgs84_pattern=_PARCEL_WGS84_ANCHOR_PATTERN,
    wgs84_anchor=_PARCEL_WGS84_ANCHOR_POINT,
    web_mercator_pattern=_PARCEL_WEB_MERCATOR_ANCHOR_PATTERN,
    web_mercator_anchor=_PARCEL_WEB_MERCATOR_ANCHOR_POINT,
)


def _polygon_geometry_type():
    """Return the polygon geometry type for QGIS 3 or QGIS 4."""
    geometry_type = getattr(Qgis, "GeometryType", None)
    if geometry_type is not None and hasattr(geometry_type, "Polygon"):
        return geometry_type.Polygon
    return getattr(QgsWkbTypes, "PolygonGeometry")


def parcel_tile_renderer():
    """Build the parcel vector tile renderer."""
    styles = QgsVectorTileBasicRenderer.simpleStyle(
        QColor(234, 179, 8, 13),
        QColor("#eab308"),
        PARCEL_TILE_STROKE_WIDTH,
        QColor("#eab308"),
        PARCEL_TILE_STROKE_WIDTH,
        QColor("#eab308"),
        QColor("#eab308"),
        1.2,
    )
    for style in styles:
        style.setLayerName(PARCEL_TILE_LAYER_NAME)
        style.setMinZoomLevel(PARCEL_TILE_MIN_ZOOM)
        style.setMaxZoomLevel(PARCEL_TILE_MAX_ZOOM)
    renderer = QgsVectorTileBasicRenderer()
    renderer.setStyles(styles)
    return renderer


def parcel_tile_labeling():
    """Label parcel vector tiles using the basic_info field."""
    label_settings = QgsPalLayerSettings()
    label_settings.fieldName = '"basic_info"'
    label_settings.isExpression = True
    configure_parcel_label_position(label_settings, _PARCEL_LABEL_POINT_EXPRESSION)
    configure_parcel_label_visibility(label_settings, label_settings.fieldName)
    label_settings.priority = 10
    obstacle_settings = label_settings.obstacleSettings()
    obstacle_settings.setIsObstacle(False)
    label_settings.setObstacleSettings(obstacle_settings)
    label_settings.setFormat(parcel_label_text_format())

    style = QgsVectorTileBasicLabelingStyle()
    style.setStyleName("Parcel labels")
    style.setLayerName(PARCEL_TILE_LAYER_NAME)
    style.setFilterExpression(
        "CASE WHEN regexp_match(\"parcel_code\", '{wgs84_pattern}') "
        "THEN intersects($geometry, {wgs84_anchor}) "
        "WHEN regexp_match(\"parcel_code\", '{web_mercator_pattern}') "
        "THEN intersects($geometry, {web_mercator_anchor}) ELSE true END".format(
            wgs84_pattern=_PARCEL_WGS84_ANCHOR_PATTERN,
            wgs84_anchor=_PARCEL_WGS84_ANCHOR_POINT,
            web_mercator_pattern=_PARCEL_WEB_MERCATOR_ANCHOR_PATTERN,
            web_mercator_anchor=_PARCEL_WEB_MERCATOR_ANCHOR_POINT,
        )
    )
    style.setGeometryType(_polygon_geometry_type())
    style.setMinZoomLevel(PARCEL_TILE_MIN_ZOOM)
    style.setMaxZoomLevel(PARCEL_TILE_MAX_ZOOM)
    style.setEnabled(True)
    style.setLabelSettings(label_settings)

    labeling = QgsVectorTileBasicLabeling()
    labeling.setStyles([style])
    return labeling
