# Tests

`run_tests.py` starts a headless QGIS, loads the plugin, and drives it against a local mock server, so no real AI provider is called. Settings, keys and chats go to a temporary folder that is deleted afterwards. Exit code 0 means every check passed.

**Linux, in the official QGIS image** (this is what CI runs):

```sh
docker run --rm -v "$PWD":/plugin -w /plugin qgis/qgis:ltr \
  sh -c "QT_QPA_PLATFORM=offscreen python3 tests/run_tests.py"
```

**Linux, with QGIS installed:**

```sh
QT_QPA_PLATFORM=offscreen python3 tests/run_tests.py
```

**macOS**, using the Python that ships with QGIS:

```sh
R=/Applications/QGIS.app/Contents/Resources
PYTHONHOME=$R PYTHONPATH=$R/python3.11:$R/python3.11/site-packages:$R/python3.11/lib-dynload:$R/python \
  /Applications/QGIS.app/Contents/MacOS/python3.12 tests/run_tests.py
```

**Windows**, in the OSGeo4W Shell:

```bat
set QT_QPA_PLATFORM=offscreen
python-qgis tests\run_tests.py
```

Some paths depend on the QGIS version installed. If QGIS is installed somewhere unusual, set `QGIS_PREFIX_PATH`.
