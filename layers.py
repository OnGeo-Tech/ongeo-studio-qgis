"""QGIS parcel layers used by OnGeo US Parcel Explorer."""

import json

from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsJsonUtils,
    QgsMessageLog,
    QgsPalLayerSettings,
    QgsProperty,
    QgsProject,
    QgsSimpleFillSymbolLayer,
    QgsSymbol,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from .config import (
    ONGEO_COLOR_FILL_RGBA,
    ONGEO_COLOR_STROKE_RGBA,
    ONGEO_STROKE_WIDTH,
    PARCEL_CRS_EPSG,
    ZOOM_BUFFER_RATIO,
)
from .qt_compat import MESSAGE_WARNING, qgs_field_type

PARCEL_LAYER_NAME = "OnGeo USA Parcels"
PARCEL_LAYER_PROPERTY = "ongeo_us_parcel_search_layer"
PARCEL_LAYER_NEXT_ID_PROPERTY = "ongeo_us_parcel_search_next_id"
FIELD_PARCEL_ID = "Parcel ID"
FIELD_ALTERNATE_PARCEL_ID = "Alternate Parcel ID"
PARCEL_LABEL_POINT_EXPRESSION = "point_on_surface($geometry)"
PARCEL_LABEL_MAX_OVERFLOW = 1.2
PARCEL_LABEL_CHARACTER_WIDTH_M = 0.00168
PARCEL_LABEL_HORIZONTAL_BUFFER_M = 0.00336
PARCEL_LABEL_HEIGHT_M = 0.00558


def _point_geometry_type():
    """Return the point geometry type for QGIS 3 or QGIS 4."""
    geometry_type = getattr(Qgis, "GeometryType", None)
    if geometry_type is not None and hasattr(geometry_type, "Point"):
        return geometry_type.Point
    return getattr(QgsWkbTypes, "PointGeometry")


def _over_point_label_placement():
    """Return point label placement for QGIS 3 or QGIS 4."""
    placement = getattr(Qgis, "LabelPlacement", None)
    if placement is not None and hasattr(placement, "OverPoint"):
        return placement.OverPoint
    return getattr(QgsPalLayerSettings, "OverPoint")


def _show_label_property():
    """Return the label visibility property for QGIS 3 or QGIS 4."""
    property_enum = getattr(QgsPalLayerSettings, "Property", None)
    if property_enum is not None and hasattr(property_enum, "Show"):
        return property_enum.Show
    return getattr(QgsPalLayerSettings, "Show")


def parcel_label_text_format():
    """Build the shared text style for selected parcels and parcel tiles."""
    text_format = QgsTextFormat()
    text_format.setFont(QFont("Arial", 9))
    text_format.setSize(9)
    text_format.setColor(QColor("#1f1f1f"))

    buffer_settings = QgsTextBufferSettings()
    buffer_settings.setEnabled(True)
    buffer_settings.setSize(1.2)
    buffer_settings.setColor(QColor("#ffffff"))
    text_format.setBuffer(buffer_settings)
    return text_format


def configure_parcel_label_position(
    label_settings, geometry_expression=PARCEL_LABEL_POINT_EXPRESSION
):
    """Place a parcel label at an interior point in QGIS 3 and QGIS 4."""
    label_settings.placement = _over_point_label_placement()
    label_settings.geometryGeneratorEnabled = True
    label_settings.geometryGeneratorType = _point_geometry_type()
    label_settings.geometryGenerator = geometry_expression


def configure_parcel_label_visibility(label_settings, label_expression):
    """Hide labels that are materially larger than their parcel at this scale."""
    expression = """(
        (length({label}) * {character_width} + {horizontal_buffer}) * @map_scale
            <= bounds_width($geometry) * {max_overflow}
        AND {label_height} * @map_scale
            <= bounds_height($geometry) * {max_overflow}
    )""".format(
        label=label_expression,
        character_width=PARCEL_LABEL_CHARACTER_WIDTH_M,
        horizontal_buffer=PARCEL_LABEL_HORIZONTAL_BUFFER_M,
        label_height=PARCEL_LABEL_HEIGHT_M,
        max_overflow=PARCEL_LABEL_MAX_OVERFLOW,
    )
    properties = label_settings.dataDefinedProperties()
    properties.setProperty(
        _show_label_property(),
        QgsProperty.fromExpression(expression),
    )
    label_settings.setDataDefinedProperties(properties)


def _geometry_from_geojson(geometry):
    """Convert GeoJSON geometry to QGIS geometry."""
    if not geometry:
        return None

    if isinstance(geometry, str):
        geometry_text = geometry
        geometry_dict = None
    else:
        geometry_dict = geometry
        geometry_text = json.dumps(geometry)

    try:
        geom = QgsJsonUtils.geometryFromGeoJson(geometry_text)
        if geom is not None and not geom.isNull():
            return geom
    except AttributeError:
        # Older PyQGIS releases may not expose this conversion method.
        pass
    except Exception as exc:
        QgsMessageLog.logMessage(
            "GeoJSON conversion by QgsJsonUtils failed: {}".format(exc),
            "OnGeo", MESSAGE_WARNING,
        )

    if geometry_dict is None:
        try:
            geometry_dict = json.loads(geometry)
        except Exception:
            return None

    try:
        feature_text = json.dumps({
            "type": "Feature",
            "geometry": geometry_dict,
            "properties": {},
        })
        try:
            features = QgsJsonUtils.stringToFeatureList(feature_text, QgsFields(), None)
        except TypeError:
            features = QgsJsonUtils.stringToFeatureList(feature_text, QgsFields())
        if features:
            geom = features[0].geometry()
            if geom is not None and not geom.isNull():
                return geom
    except Exception as exc:
        QgsMessageLog.logMessage(
            "GeoJSON feature conversion failed: {}".format(exc),
            "OnGeo", MESSAGE_WARNING,
        )

    try:
        geom = _geometry_from_coordinates(geometry_dict)
        if geom is not None and not geom.isNull():
            return geom
    except Exception as exc:
        QgsMessageLog.logMessage(
            "Fallback parcel geometry conversion failed: {}".format(exc),
            "OnGeo", MESSAGE_WARNING,
        )

    return None


def _point_to_wkt(point):
    """Convert a GeoJSON point to WKT coordinates."""
    return "{} {}".format(float(point[0]), float(point[1]))


def _ring_to_wkt(ring):
    """Convert a GeoJSON ring to WKT."""
    points = list(ring or [])
    if len(points) < 3:
        return None
    first = points[0]
    last = points[-1]
    if len(first) >= 2 and len(last) >= 2 and (float(first[0]) != float(last[0]) or float(first[1]) != float(last[1])):
        points.append(first)
    return "(" + ", ".join(_point_to_wkt(point) for point in points) + ")"


def _polygon_to_wkt(polygon):
    """Convert a GeoJSON polygon to WKT."""
    rings = []
    for ring in polygon or []:
        ring_text = _ring_to_wkt(ring)
        if ring_text:
            rings.append(ring_text)
    if not rings:
        return None
    return "(" + ", ".join(rings) + ")"


def _geometry_from_coordinates(geometry_dict):
    """Parse GeoJSON coordinates when the QGIS parser is unavailable."""
    geometry_type = str((geometry_dict or {}).get("type") or "")
    coords = (geometry_dict or {}).get("coordinates")
    if geometry_type == "Polygon":
        polygon_text = _polygon_to_wkt(coords)
        if not polygon_text:
            return None
        return QgsGeometry.fromWkt("MULTIPOLYGON ({})".format(polygon_text))
    if geometry_type == "MultiPolygon":
        polygons = []
        for polygon in coords or []:
            polygon_text = _polygon_to_wkt(polygon)
            if polygon_text:
                polygons.append(polygon_text)
        if not polygons:
            return None
        return QgsGeometry.fromWkt("MULTIPOLYGON ({})".format(", ".join(polygons)))
    return None


def _looks_like_parcel_id(value):
    """Check whether a value resembles a parcel identifier."""
    text = str(value or "").strip()
    return bool(text) and (any(char.isdigit() for char in text) or "_" in text)


def _label_parts(label):
    """Split an API label into parcel and location fields."""
    parts = [p.strip() for p in str(label or "").split(",") if p.strip()]
    alternate_parcel_no = ""
    location_parts = parts[1:]
    if len(parts) > 1 and _looks_like_parcel_id(parts[1]):
        alternate_parcel_no = parts[1]
        location_parts = parts[2:]
    location = _label_location_parts(location_parts)
    return {
        "parcel_no": parts[0] if parts else "",
        "alternate_parcel_no": alternate_parcel_no,
        "state": location.get("state") or "",
        "county": location.get("county") or "",
        "place": location.get("place") or "",
    }


def _label_location_parts(parts):
    """Read location fields from the end of an API label."""
    parts = list(parts or [])
    while parts and parts[-1].lower() in ("usa", "united states", "united states of america"):
        parts.pop()
    return {
        "state": parts[0] if len(parts) > 0 else "",
        "county": parts[1] if len(parts) > 1 else "",
        "place": parts[-1] if len(parts) > 2 else "",
    }


def parcel_number(suggestion):
    """Return the parcel number from a search result."""
    suggestion = suggestion or {}
    for name in ("parcelNo", "parcelNumber"):
        value = suggestion.get(name)
        if value:
            return str(value)
    return _label_parts(suggestion.get("label") or "").get("parcel_no") or ""


def alternate_parcel_number(suggestion, parsed_label=None):
    """Return the alternate parcel number from a search result."""
    suggestion = suggestion or {}
    for name in ("alternateParcelId", "alternateParcelID", "alternateParcelNo"):
        value = suggestion.get(name)
        if value:
            return str(value)
    parsed_label = parsed_label or _label_parts(suggestion.get("label") or "")
    return parsed_label.get("alternate_parcel_no") or ""


def result_addresses(result):
    """Return nonempty addresses from a search result."""
    addresses = (result or {}).get("addresses") or []
    return [str(address) for address in addresses if address]


def result_location_text(result):
    """Return the location text from a search result."""
    result = result or {}
    location = str(result.get("location") or "").strip()
    if location:
        return location
    parts = _label_parts(result.get("label") or "")
    return ", ".join([p for p in (parts.get("place"), parts.get("county"), parts.get("state")) if p])


def _result_location_parts(result):
    """Return state, county, and place from either API result format."""
    result = result or {}
    location = str(result.get("location") or "").strip()
    if not location:
        return _label_parts(result.get("label") or "")
    parts = [part.strip() for part in location.split(",") if part.strip()]
    return {
        "state": parts[-1] if len(parts) >= 1 else "",
        "county": parts[-2] if len(parts) >= 2 else "",
        "place": ", ".join(parts[:-2]) if len(parts) >= 3 else "",
    }


def result_compact_text(result):
    """Build a compact search result label."""
    result = result or {}
    parcel_no = parcel_number(result)
    addresses = result_addresses(result)
    if parcel_no and addresses:
        return "{} - {}".format(parcel_no, addresses[0])
    if addresses:
        return addresses[0]
    return parcel_no or str(result.get("label") or "Parcel")


def apply_parcel_style(layer):
    """Apply the OnGeo parcel style; report rendering failures in QGIS logs."""
    try:
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            QgsMessageLog.logMessage(
                "Could not apply parcel style to '{}': no default symbol.".format(layer.name()),
                "OnGeo", MESSAGE_WARNING,
            )
            return
        fill = QgsSimpleFillSymbolLayer()
        fill.setFillColor(QColor(*ONGEO_COLOR_FILL_RGBA))
        fill.setStrokeColor(QColor(*ONGEO_COLOR_STROKE_RGBA))
        fill.setStrokeWidth(ONGEO_STROKE_WIDTH)
        symbol.changeSymbolLayer(0, fill)
        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()
    except Exception as exc:
        QgsMessageLog.logMessage(
            "Could not apply parcel style to '{}': {}".format(layer.name(), exc),
            "OnGeo", MESSAGE_WARNING,
        )


def apply_parcel_labels(layer):
    """Label selected parcels by Parcel ID; report failures in QGIS logs."""
    try:
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = '"{}"'.format(FIELD_PARCEL_ID)
        label_settings.isExpression = True
        configure_parcel_label_position(label_settings)
        configure_parcel_label_visibility(label_settings, label_settings.fieldName)
        label_settings.setFormat(parcel_label_text_format())

        layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
    except Exception as exc:
        QgsMessageLog.logMessage(
            "Could not apply parcel labels to '{}': {}".format(layer.name(), exc),
            "OnGeo", MESSAGE_WARNING,
        )


def _parcel_fields():
    """Define fields for a selected parcel layer."""
    fields = QgsFields()
    fields.append(QgsField("id", qgs_field_type("Int")))
    fields.append(QgsField(FIELD_PARCEL_ID, qgs_field_type("String")))
    fields.append(QgsField(FIELD_ALTERNATE_PARCEL_ID, qgs_field_type("String")))
    fields.append(QgsField("Addresses", qgs_field_type("String")))
    fields.append(QgsField("State", qgs_field_type("String")))
    fields.append(QgsField("County", qgs_field_type("String")))
    fields.append(QgsField("Place", qgs_field_type("String")))
    return fields


def _ensure_parcel_fields(layer):
    """Add missing supported fields to an existing parcel layer."""
    existing = set(layer.fields().names())
    missing = []
    for field in _parcel_fields():
        if field.name() not in existing:
            missing.append(field)
    if missing:
        layer.dataProvider().addAttributes(missing)
        layer.updateFields()


def _null_if_empty(value):
    """Map empty strings to NULL attributes."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _next_parcel_id(layer):
    """Return the next feature ID in the parcel layer."""
    if layer.featureCount() == 0:
        return 1
    stored_value = layer.customProperty(PARCEL_LAYER_NEXT_ID_PROPERTY, None)
    if stored_value is not None:
        try:
            stored_id = int(stored_value)
            if stored_id > 0:
                return stored_id
        except (TypeError, ValueError):
            pass

    field_index = layer.fields().indexOf("id")
    feature_count = 0
    max_id = 0
    if field_index < 0:
        return 1
    for feature in layer.getFeatures():
        feature_count += 1
        value = feature[field_index]
        if value is None:
            continue
        try:
            max_id = max(max_id, int(value))
        except (TypeError, ValueError):
            pass
    if max_id > 0:
        return max_id + 1
    return feature_count + 1


def _parcel_feature(fields, geom, suggestion, parcel_id):
    """Build a parcel feature with its attributes."""
    suggestion = suggestion or {}
    location_parts = _result_location_parts(suggestion)
    addresses = result_addresses(suggestion)
    values = {
        "id": parcel_id,
        FIELD_PARCEL_ID: parcel_number(suggestion),
        FIELD_ALTERNATE_PARCEL_ID: alternate_parcel_number(suggestion),
        "Addresses": "\n".join(addresses),
        "State": location_parts.get("state"),
        "County": location_parts.get("county"),
        "Place": location_parts.get("place"),
    }
    feat = QgsFeature(fields)
    feat.initAttributes(fields.count())
    feat.setGeometry(geom)
    attrs = [None] * fields.count()
    for name, value in values.items():
        index = fields.indexOf(name)
        if index >= 0:
            attrs[index] = _null_if_empty(value)
    feat.setAttributes(attrs)
    return feat


def _make_parcel_layer(crs):
    """Create the shared selected parcel layer."""
    layer_crs_id = crs.authid() or PARCEL_CRS_EPSG
    layer = QgsVectorLayer("MultiPolygon?crs=" + layer_crs_id, PARCEL_LAYER_NAME, "memory")
    layer.setCustomProperty(PARCEL_LAYER_PROPERTY, True)
    _ensure_parcel_fields(layer)
    apply_parcel_style(layer)
    apply_parcel_labels(layer)
    return layer


def _prepare_geometry(details, target_layer=None):
    """Read parcel geometry and transform it to the target layer CRS."""
    geometry = (details or {}).get("geometry")
    if not geometry:
        raise ValueError("Parcel details response has no geometry.")

    geom = _geometry_from_geojson(geometry)
    if geom is None or geom.isEmpty():
        raise ValueError("Could not parse parcel geometry.")
    geom.convertToMultiType()

    layer_crs = QgsCoordinateReferenceSystem(PARCEL_CRS_EPSG)
    if target_layer is not None and target_layer.isValid():
        target_crs = target_layer.crs()
        if layer_crs.isValid() and target_crs.isValid() and layer_crs != target_crs:
            transform = QgsCoordinateTransform(layer_crs, target_crs, QgsProject.instance())
            geom.transform(transform)
        layer_crs = target_crs
    return geom, layer_crs


def _matching_parcel_geometry(layer, suggestion):
    """Return an existing geometry for the same parcel, state, and county."""
    if layer is None or not layer.isValid():
        return None
    location_parts = _result_location_parts(suggestion)
    parcel = parcel_number(suggestion)
    state = location_parts.get("state") or ""
    county = location_parts.get("county") or ""
    if not parcel or not state or not county:
        return None
    for feature in layer.getFeatures():
        try:
            if (
                str(feature[FIELD_PARCEL_ID] or "").strip() == parcel
                and str(feature["State"] or "").strip() == state
                and str(feature["County"] or "").strip() == county
            ):
                return feature.geometry()
        except (KeyError, RuntimeError):
            continue
    return None


def _append_prepared_parcel(project, geom, layer_crs, suggestion=None, target_layer=None):
    """Append the prepared parcel geometry to a layer."""
    new_layer = target_layer is None or not target_layer.isValid()
    layer = target_layer if not new_layer else _make_parcel_layer(layer_crs)
    layer.setCustomProperty(PARCEL_LAYER_PROPERTY, True)
    _ensure_parcel_fields(layer)
    parcel_id = _next_parcel_id(layer)
    feat = _parcel_feature(layer.fields(), geom, suggestion, parcel_id)
    was_editable = layer.isEditable()
    if not was_editable and not layer.startEditing():
        raise ValueError("Could not start editing the parcel layer.")
    try:
        if not layer.addFeature(feat):
            raise ValueError("Could not add parcel feature.")
        if not was_editable and not layer.commitChanges():
            raise ValueError("Could not save the parcel feature.")
    except Exception:
        if not was_editable and layer.isEditable():
            layer.rollBack()
        raise

    layer.updateExtents()
    layer.setCustomProperty(PARCEL_LAYER_NEXT_ID_PROPERTY, parcel_id + 1)
    if layer.id() not in project.mapLayers():
        project.addMapLayer(layer, False)
    elif not new_layer:
        layer.triggerRepaint()
    place_layer_at_top(project, layer)
    return layer


def place_layer_at_top(project, layer):
    """Place an activated OnGeo layer above all other project layers."""
    root = project.layerTreeRoot()
    node = root.findLayer(layer.id())
    if node is None:
        root.insertLayer(0, layer)
    elif root.children()[0] is not node:
        parent = node.parent()
        root.insertChildNode(0, node.clone())
        parent.removeChildNode(node)
    if root.hasCustomLayerOrder():
        current = root.customLayerOrder()
        root.setCustomLayerOrder(
            [layer] + [other for other in current if other.id() != layer.id()]
        )


def append_parcel_to_layer(project, details, suggestion=None, target_layer=None):
    """Append a parcel and return its geometry for map navigation."""
    geom, layer_crs = _prepare_geometry(details, target_layer)
    existing = _matching_parcel_geometry(target_layer, suggestion)
    if existing is not None and not existing.isEmpty():
        return target_layer, existing, target_layer.crs()
    layer = _append_prepared_parcel(project, geom, layer_crs, suggestion, target_layer)
    return layer, geom, layer_crs


def zoom_to_geometry(canvas, geom, source_crs, buffer_ratio=ZOOM_BUFFER_RATIO):
    """Zoom the map canvas to a geometry."""
    if not canvas or geom is None or geom.isEmpty():
        return
    extent = geom.boundingBox()
    if extent.isNull() or extent.isEmpty():
        return
    target_crs = canvas.mapSettings().destinationCrs()
    if source_crs.isValid() and target_crs.isValid() and source_crs != target_crs:
        transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
        extent = transform.transformBoundingBox(extent)
        if extent.isNull() or extent.isEmpty():
            return
    if buffer_ratio and buffer_ratio > 0:
        extent.scale(1.0 + (float(buffer_ratio) * 2.0))
    canvas.setExtent(extent)
    canvas.refresh()


