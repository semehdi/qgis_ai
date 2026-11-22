"""QGIS AI Assistant - chat with any AI model from inside QGIS."""


def classFactory(iface):  # noqa: N802 (QGIS naming)
    from .plugin import QgisAiAssistantPlugin

    return QgisAiAssistantPlugin(iface)
