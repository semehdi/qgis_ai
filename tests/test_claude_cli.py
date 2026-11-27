"""Cross-platform checks of how the Claude Code CLI is found and started (claude_path.py).

Plain Python, no QGIS needed, so CI runs it on Windows, macOS and Linux. Fake `claude`
programs are installed the ways the real one is (npm package run by node, npm shim on
Windows, native executable, batch file), then really started with the command
claude_path builds; each one reports back the arguments, stdin and environment it got.

    python tests/test_claude_cli.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("claude_path", os.path.join(HERE, "..", "claude_path.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

WINDOWS = cp.WINDOWS
FAILS = []
# Arguments that break naive quoting: empty, spaces, quotes, accents.
ARGS = ["-p", "--tools", "", "--system-prompt-file", "C:\\dossier été\\system prompt.txt",
        "--model", "opus", 'say "hi"']
STDIN = json.dumps({"type": "user", "message": {"content": "Carte de l'Algérie ✓ 地图"}},
                   ensure_ascii=False) + "\n"

FAKE_PY = r'''import json, os, sys
data = sys.stdin.buffer.read().decode("utf-8")
print(json.dumps({"type": "result", "argv": sys.argv[1:], "stdin": data,
                  "api_key": os.environ.get("ANTHROPIC_API_KEY"), "kind": "python"}))
'''
FAKE_JS = r'''#!/usr/bin/env node
let data = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", c => data += c);
process.stdin.on("end", () => console.log(JSON.stringify({type: "result", argv: process.argv.slice(2),
  stdin: data, api_key: process.env.ANTHROPIC_API_KEY || null, kind: "node"})));
'''
NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
'''


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: {}".format(detail)))
    if not cond:
        FAILS.append(name)


def write(path, text, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n" if not path.endswith(".cmd") else "\r\n") as fh:
        fh.write(text)
    if executable and not WINDOWS:
        os.chmod(path, 0o755)


def run(exe, environ):
    """Start exe the way the plugin does and return what the fake reported."""
    program, args, native = cp.launch(exe, ARGS, environ)
    env = cp.child_env(environ, exe)
    if native is not None:
        cmd = '"{}" {}'.format(program, native)  # passed verbatim, like QProcess.setNativeArguments
    else:
        cmd = [program] + args
    proc = subprocess.run(cmd, input=STDIN.encode("utf-8"), env=env, capture_output=True, timeout=120)
    out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        return json.loads(out[-1]), (program, args, native)
    except (IndexError, ValueError):
        return {"error": proc.stderr.decode("utf-8", "replace")[-800:], "out": out}, (program, args, native)


def same_call(name, got, how):
    check(name + ": arguments arrive intact", got.get("argv") == ARGS, (got, how))
    check(name + ": stdin arrives intact (UTF-8)", got.get("stdin") == STDIN, (got.get("stdin"), how))
    check(name + ": no API key passed", got.get("api_key") is None, got.get("api_key"))


def main():
    print("Platform:", sys.platform, "| Python", sys.version.split()[0])
    tmp = tempfile.mkdtemp(prefix="claude cli é ")
    base_env = dict(os.environ)
    base_env["ANTHROPIC_API_KEY"] = "sk-must-not-leak"
    base_env["CLAUDECODE"] = "1"
    try:
        # --- environment
        env = cp.child_env(base_env, os.path.join(tmp, "bin", "claude"))
        check("API key and parent session variables removed",
              not any(k.upper() in cp.DROP_ENV for k in env), [k for k in env if k.upper() in cp.DROP_ENV])
        check("PATH kept", any(k.upper() == "PATH" and v for k, v in env.items()))

        # --- npm package run by node (Windows: claude.cmd shim; Unix: bin/claude -> cli.js)
        node = shutil.which("node")
        if node:
            if WINDOWS:
                prefix = os.path.join(tmp, "npm")
                exe = os.path.join(prefix, "claude.cmd")
                write(os.path.join(prefix, "node_modules", "@anthropic-ai", "claude-code", "cli.js"), FAKE_JS)
                write(exe, NPM_SHIM)
            else:
                prefix = os.path.join(tmp, "npm global")
                js = os.path.join(prefix, "lib", "node_modules", "@anthropic-ai", "claude-code", "cli.js")
                write(js, FAKE_JS, executable=True)
                exe = os.path.join(prefix, "bin", "claude")
                os.makedirs(os.path.dirname(exe))
                os.symlink(js, exe)
                os.symlink(node, os.path.join(prefix, "bin", "node"))  # as nvm installs it
            got, how = run(exe, base_env)
            check("npm install: started through node, not a shell", got.get("kind") == "node"
                  and os.path.basename(how[0]).lower().startswith("node") and how[2] is None, (got, how))
            same_call("npm install", got, how)
            if not WINDOWS:
                # QGIS started from the Finder / a desktop launcher: node isn't on PATH.
                bare = dict(base_env, PATH="/usr/bin:/bin")
                got, how = run(exe, bare)
                check("npm install works without node on PATH", got.get("kind") == "node", (got, how))
        else:
            print("SKIP npm install checks: node is not installed")

        # --- native executable (here a Python script standing in for it) or batch file
        if WINDOWS:
            script = os.path.join(tmp, "fake claude.py")
            write(script, FAKE_PY)
            exe = os.path.join(tmp, "other install", "claude.bat")
            write(exe, '@"{}" "{}" %*\n'.format(sys.executable, script))
            got, how = run(exe, base_env)
            check("batch file: run through cmd.exe with a native command line", how[2] is not None, how)
            same_call("batch file", got, how)
        else:
            exe = os.path.join(tmp, "native", "claude")
            write(exe, "#!{}\n{}".format(sys.executable, FAKE_PY), executable=True)
            got, how = run(exe, base_env)
            check("native executable started directly", how[0] == exe and how[2] is None, how)
            same_call("native executable", got, how)

        # --- finding claude
        saved = dict(os.environ)
        try:
            home = os.path.join(tmp, "home")
            os.environ["HOME"] = os.environ["USERPROFILE"] = home
            os.environ["APPDATA"] = os.path.join(home, "AppData", "Roaming")
            os.environ["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            os.environ["PATH"] = empty
            check("nothing found when not installed", cp.find_claude() in ("", cp.HOST_PREFIX + "claude"),
                  cp.find_claude())
            if WINDOWS:
                installed = os.path.join(home, "AppData", "Roaming", "npm", "claude.cmd")
                write(installed, NPM_SHIM)
            else:
                installed = os.path.join(home, ".local", "bin", "claude")
                write(installed, "#!/bin/sh\n", executable=True)
            check("found in the usual install folder without PATH", cp.find_claude() == installed,
                  cp.find_claude())
            on_path = os.path.join(tmp, "on path")
            write(os.path.join(on_path, "claude.exe" if WINDOWS else "claude"), "x", executable=True)
            os.environ["PATH"] = on_path
            if WINDOWS:
                os.environ["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
            check("found on PATH first", os.path.dirname(cp.find_claude()) == on_path, cp.find_claude())
            check("path set in Settings wins", cp.find_claude(installed) == installed)
            check("missing path in Settings: not found",
                  cp.find_claude(os.path.join(tmp, "nope", "claude")) in ("",) or cp.FLATPAK)
        finally:
            os.environ.clear()
            os.environ.update(saved)

        # --- Flatpak QGIS: claude runs on the host
        program, args, native = cp.launch(cp.HOST_PREFIX + "claude", ARGS, base_env)
        check("flatpak: started on the host with the API key unset",
              program == "flatpak-spawn" and args[0] == "--host" and args[-len(ARGS):] == ARGS
              and "ANTHROPIC_API_KEY" in args and native is None, (program, args))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n{} failed".format(len(FAILS)) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
