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

## Claude Code CLI launch, on any OS

`test_claude_cli.py` needs only Python (and Node.js for the npm cases), not QGIS. It installs fake `claude` programs the ways the real one gets installed: npm package, Windows npm shim, native executable, batch file. It then starts each one with the command the plugin builds and checks that the arguments, UTF-8 input and environment arrive intact. CI runs it on Windows, macOS and Linux.

```sh
python tests/test_claude_cli.py
```
