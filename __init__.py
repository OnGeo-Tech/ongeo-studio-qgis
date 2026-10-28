"""QGIS plugin entry point."""

def classFactory(iface):
    """Create the OnGeo US Parcel Explorer plugin."""
    from .plugin import OnGeoUSParcelSearchPlugin
    return OnGeoUSParcelSearchPlugin(iface)
