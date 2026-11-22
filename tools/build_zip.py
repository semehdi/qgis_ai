"""Build qgis_assistant.zip for "Install from ZIP" - works on Windows, macOS and Linux.

Usage (from anywhere):  python tools/build_zip.py
The zip is written to dist/qgis_assistant.zip in the repository.
"""

import os
import zipfile

PLUGIN_NAME = "qgis_assistant"  # plugin id: the folder name QGIS expects inside the zip
# (not "qgis_ai_assistant": another plugin already uses that id on plugins.qgis.org)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE_EXT = (".py", ".svg", ".txt")
EXCLUDE_DIRS = {"tools", "dist", "build", "__pycache__", ".git", ".vscode", ".idea"}


def main():
    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, PLUGIN_NAME + ".zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(ROOT)):
            path = os.path.join(ROOT, name)
            wanted = name.endswith(INCLUDE_EXT) or name == "LICENSE"
            if os.path.isfile(path) and wanted and not name.startswith("."):
                # Forward slashes inside the zip on every OS.
                zf.write(path, PLUGIN_NAME + "/" + name)
                count += 1
        # Interface translations
        i18n_dir = os.path.join(ROOT, "i18n")
        for name in sorted(os.listdir(i18n_dir)):
            if name.endswith(".json"):
                zf.write(os.path.join(i18n_dir, name), PLUGIN_NAME + "/i18n/" + name)
                count += 1
    print("Wrote {} ({} files)".format(out, count))


if __name__ == "__main__":
    main()
