"""Describe the current QGIS project so the model knows what it is working with."""

from qgis.core import Qgis, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, Qt

from .compat import qenum

MAX_LAYERS = 60
MAX_FIELDS = 40
MAX_CHARS = 15000


def qgis_version():
    try:
        return Qgis.version()
    except AttributeError:
        return Qgis.QGIS_VERSION


def _fmt_extent(rect):
    return "{:.6g}, {:.6g} : {:.6g}, {:.6g}".format(
        rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())


def build_context(iface):
    project = QgsProject.instance()
    canvas = iface.mapCanvas()
    lines = [
        "QGIS version: {}".format(qgis_version()),
        "Project file: {}".format(project.fileName() or "(unsaved)"),
        "Project CRS: {}".format(project.crs().authid() or project.crs().description()),
        "Map view: extent {} in {}, scale 1:{:,.0f}".format(
            _fmt_extent(canvas.extent()), canvas.mapSettings().destinationCrs().authid(),
            canvas.scale()),
    ]
    active = iface.activeLayer()
    lines.append("Active layer: {}".format(
        "{} (id {})".format(active.name(), active.id()) if active else "none"))

    nodes = project.layerTreeRoot().findLayers()
    lines.append("Layers ({} total, top of the layer panel first):".format(len(nodes)))
    for node in nodes[:MAX_LAYERS]:
        layer = node.layer()
        if layer is None:
            continue
        lines.extend(_describe_layer(layer, node.isVisible()))
    if len(nodes) > MAX_LAYERS:
        lines.append("- ... {} more layers not listed".format(len(nodes) - MAX_LAYERS))

    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n... (context truncated)"
    return text


def _describe_layer(layer, visible):
    head = "- \"{}\" id={} {} provider={} crs={}".format(
        layer.name(), layer.id(), "visible" if visible else "hidden",
        layer.providerType(), layer.crs().authid())
    out = [head]
    if isinstance(layer, QgsVectorLayer):
        out[0] += " vector {} features={} selected={}".format(
            QgsWkbTypes.displayString(layer.wkbType()), layer.featureCount(),
            layer.selectedFeatureCount())
        fields = layer.fields()
        names = ["{}:{}".format(f.name(), f.typeName()) for f in list(fields)[:MAX_FIELDS]]
        if fields.count() > MAX_FIELDS:
            names.append("... +{} more".format(fields.count() - MAX_FIELDS))
        out.append("    fields: " + ", ".join(names))
    elif isinstance(layer, QgsRasterLayer):
        out[0] += " raster {}x{} px bands={}".format(
            layer.width(), layer.height(), layer.bandCount())
    out[0] += " extent=" + _fmt_extent(layer.extent())
    return out


def map_snapshot(iface, max_width=1280):
    """Return (base64_jpeg, mime) of the current map canvas."""
    pix = iface.mapCanvas().grab()
    if pix.width() > max_width:
        pix = pix.scaledToWidth(max_width, qenum(Qt, "TransformationMode", "SmoothTransformation"))
    # JPEG keeps requests small; PNG is the fallback for Qt builds without the JPEG plugin.
    for fmt, mime, quality in (("JPEG", "image/jpeg", 85), ("PNG", "image/png", -1)):
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(qenum(QIODevice, "OpenModeFlag", "WriteOnly"))
        ok = pix.save(buf, fmt, quality)
        buf.close()
        if ok and data.size() > 0:
            return bytes(data.toBase64()).decode("ascii"), mime
    raise RuntimeError("could not encode the map image")
