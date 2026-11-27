"""Find the Claude Code CLI and build the command that starts it, on Windows, macOS
and Linux. Plain Python (no Qt), so it can be tested on any machine.

QGIS often runs without the user's shell PATH (macOS apps started from the Finder or
the Dock, Windows shortcuts, Flatpak sandboxes), so the usual install places are
searched too, and the launch command avoids depending on PATH:

- Windows npm installs give `claude.cmd`, a batch file. Running it would need cmd.exe,
  which mangles quoted arguments and survives Stop as a stray process, so the program
  it points to (node + cli.js, or claude.exe) is started directly.
- Unix npm installs give a `#!/usr/bin/env node` script, and node may be missing from
  PATH (nvm, volta, Homebrew...): node is looked up and started explicitly.
- Flatpak QGIS can't see programs installed on the host: `flatpak-spawn --host` runs
  claude outside the sandbox, through a login shell that has the user's PATH.
"""

import glob
import os
import re
import shutil
import subprocess
import sys

WINDOWS = sys.platform.startswith("win")
FLATPAK = os.path.exists("/.flatpak-info")
HOST_PREFIX = "host:"  # find_claude() result meaning "the claude on the Flatpak host"

# Variables that would make the CLI think it runs inside another Claude Code session,
# or bill an API key instead of the user's Claude plan.
DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY")


def _home():
    return os.path.expanduser("~")


def _env(name, default):
    return os.environ.get(name) or default


def candidates():
    """Usual install places of claude, most likely first."""
    home = _home()
    if WINDOWS:
        appdata = _env("APPDATA", os.path.join(home, "AppData", "Roaming"))
        local = _env("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        found = [os.path.join(home, ".local", "bin", "claude.exe"),  # native installer
                 os.path.join(local, "Microsoft", "WinGet", "Links", "claude.exe"),
                 os.path.join(local, "Programs", "claude", "claude.exe"),
                 os.path.join(appdata, "npm", "claude.cmd"),  # npm install -g
                 os.path.join(home, "scoop", "shims", "claude.exe"),
                 os.path.join(home, ".volta", "bin", "claude.exe")]
        found += sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                               "Anthropic.ClaudeCode*", "claude.exe")), reverse=True)
        binary = "claude.exe"
    else:
        found = [os.path.join(home, ".local", "bin", "claude"),  # native installer
                 os.path.join(home, ".claude", "local", "claude"),
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude",
                 "/snap/bin/claude",
                 os.path.join(home, ".npm-global", "bin", "claude"),
                 os.path.join(home, ".volta", "bin", "claude"),
                 os.path.join(home, ".bun", "bin", "claude"),
                 os.path.join(home, ".asdf", "shims", "claude"),
                 os.path.join(home, ".local", "share", "mise", "shims", "claude")]
        found += sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin", "claude")),
                        reverse=True)
        binary = "claude"
    # The copy bundled with the Claude Code extension of VS Code and its forks, newest first.
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf", ".vscode-oss"):
        found += sorted(glob.glob(os.path.join(home, editor, "extensions", "anthropic.claude-code-*",
                                               "resources", "native-binary", binary)), reverse=True)
    return found


def _usable(path):
    return os.path.isfile(path) and (WINDOWS or os.access(path, os.X_OK))


def find_claude(custom=""):
    """Path of the claude executable, HOST_PREFIX + command inside Flatpak, or ""."""
    custom = os.path.expanduser((custom or "").strip())
    if custom:
        found = custom if os.path.isfile(custom) else shutil.which(custom)
        if found:
            return found
        return HOST_PREFIX + custom if FLATPAK and shutil.which("flatpak-spawn") else ""
    found = shutil.which("claude")
    if found:
        return found
    for path in candidates():
        if _usable(path):
            return path
    if FLATPAK and shutil.which("flatpak-spawn"):
        return HOST_PREFIX + "claude"
    return ""


def extra_path(exe):
    """Folders to put first on PATH, so the CLI (or the node it runs on) is found."""
    dirs = [os.path.dirname(exe)] if exe and not exe.startswith(HOST_PREFIX) else []
    real = os.path.realpath(exe) if dirs else ""
    if real and os.path.dirname(real) not in dirs:
        dirs.append(os.path.dirname(real))
    home = _home()
    if WINDOWS:
        dirs += [os.path.join(_env("ProgramFiles", r"C:\Program Files"), "nodejs"),
                 os.path.join(_env("APPDATA", os.path.join(home, "AppData", "Roaming")), "npm"),
                 os.path.join(home, ".volta", "bin")]
    else:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, ".local", "bin"),
                 os.path.join(home, ".volta", "bin")]
        dirs += sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")), reverse=True)
    return [d for d in dirs if d and os.path.isdir(d)]


def child_env(environ, exe):
    """Environment for the CLI: the user's, minus DROP_ENV, with extra_path() in front."""
    env = {k: v for k, v in environ.items() if k.upper() not in DROP_ENV}
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[path_key] = os.pathsep.join(extra_path(exe) + [env.get(path_key, "")])
    if WINDOWS and not any(k.upper() == "CLAUDE_CODE_GIT_BASH_PATH" for k in env):
        # Claude Code for Windows looks for Git Bash; point it at the usual place.
        for root in (_env("ProgramFiles", r"C:\Program Files"), _env("ProgramW6432", r"C:\Program Files"),
                     os.path.join(_env("LOCALAPPDATA", ""), "Programs")):
            bash = os.path.join(root, "Git", "bin", "bash.exe")
            if root and os.path.isfile(bash):
                env["CLAUDE_CODE_GIT_BASH_PATH"] = bash
                break
    return env


_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+)"', re.I)


def _npm_shim_target(cmd_path):
    """The program a Windows npm shim (claude.cmd) starts: a .js file or an .exe."""
    try:
        with open(cmd_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    base = os.path.dirname(cmd_path)
    for rel in _SHIM_TARGET.findall(text):
        if rel.lower().endswith("node.exe"):
            continue
        target = os.path.normpath(os.path.join(base, rel))
        if os.path.isfile(target):
            return target
    return ""


def _node(exe, environ):
    """node next to the CLI (as npm on Windows and nvm put it), else on PATH."""
    for d in extra_path(exe):
        for name in (("node.exe",) if WINDOWS else ("node",)):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
    path = next((v for k, v in environ.items() if k.upper() == "PATH"), "")
    return shutil.which("node", path=os.pathsep.join(extra_path(exe) + [path])) or ""


def launch(exe, args, environ=None):
    """(program, arguments, native) to start claude with args.

    native is a complete Windows command line for cmd.exe, only for batch files that
    aren't npm shims; it must be passed as is (QProcess.setNativeArguments)."""
    environ = os.environ if environ is None else environ
    args = list(args)
    if exe.startswith(HOST_PREFIX):
        command = exe[len(HOST_PREFIX):]
        unset = [a for name in DROP_ENV for a in ("-u", name)]
        return "flatpak-spawn", (["--host", "--watch-bus", "env"] + unset
                                 + ["sh", "-lc", 'exec "$0" "$@"', command] + args), None
    target = exe
    if WINDOWS and exe.lower().endswith((".cmd", ".bat")):
        target = _npm_shim_target(exe)
        if not target:
            comspec = environ.get("COMSPEC") or "cmd.exe"
            line = subprocess.list2cmdline([exe] + args)
            return comspec, [], '/d /s /c "{}"'.format(line)
    if target.lower().endswith((".js", ".mjs", ".cjs")) or \
            (not WINDOWS and os.path.realpath(target).endswith((".js", ".mjs", ".cjs"))):
        node = _node(exe, environ)
        if node:
            return node, [os.path.realpath(target)] + args, None
    return target, args, None
