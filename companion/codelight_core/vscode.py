from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from typing import Callable


# CLI name → user settings.json path (Linux)
VSCODE_FLAVORS = [
    ("code", "~/.config/Code/User/settings.json"),
    ("code-insiders", "~/.config/Code - Insiders/User/settings.json"),
    ("codium", "~/.config/VSCodium/User/settings.json"),
]
VSCODE_EXT_ID = "sensnology.codelight"
AGENT_EXECUTABLES = {
    "claude": ("claude",),
    "copilot": ("copilot",),
    "codex": ("codex",),
}
AGENT_VSCODE_EXTENSIONS = {
    "claude": {"anthropic.claude-code"},
    "copilot": {"github.copilot", "github.copilot-chat"},
    "codex": {"openai.chatgpt"},
}


def detect_installed_agents(
    *,
    agent_executables: dict[str, tuple[str, ...]] | None = None,
    agent_vscode_extensions: dict[str, set[str]] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run=subprocess.run,
) -> set[str]:
    """Detect supported agents from CLIs and local VSCode extensions."""
    executables_by_agent = agent_executables or AGENT_EXECUTABLES
    extensions_by_agent = agent_vscode_extensions or AGENT_VSCODE_EXTENSIONS
    detected = {
        agent for agent, executables in executables_by_agent.items()
        if any(which(exe) for exe in executables)
    }
    installed_extensions: set[str] = set()
    for cli, _ in VSCODE_FLAVORS:
        exe = which(cli)
        if not exe:
            continue
        try:
            result = run([exe, "--list-extensions"],
                         capture_output=True, text=True, timeout=15)
            installed_extensions.update(
                line.strip().lower() for line in result.stdout.splitlines()
                if line.strip()
            )
        except Exception:
            continue
    for agent, extension_ids in extensions_by_agent.items():
        if installed_extensions.intersection(extension_ids):
            detected.add(agent)
    return detected


def parse_agent_set(value: str | None, supported: set[str]) -> set[str]:
    if not value:
        return set()
    return {
        item.strip().lower() for item in value.split(",")
        if item.strip().lower() in supported
    }


def find_vscode_cli(
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, str] | None:
    """Return (cli_path, settings_path) for the first VSCode flavor found."""
    for cli, settings in VSCODE_FLAVORS:
        exe = which(cli)
        if exe:
            return exe, os.path.expanduser(settings)
    return None


def configure_vscode_settings(settings_path: str, secret: str,
                              ws_port: int) -> None:
    """Write codelight.* keys into VSCode user settings."""
    settings = {}
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        pass
    except Exception:
        print(f"[vscode] could not parse {settings_path} (comments?) — set "
              f"codelight.secret = {secret!r} manually", file=sys.stderr)
        return

    desired = {"codelight.secret": secret}
    if ws_port != 8765:
        desired["codelight.port"] = ws_port
    if all(settings.get(k) == v for k, v in desired.items()):
        return
    settings.update(desired)

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=4)
        f.write("\n")
    print(f"[vscode] configured codelight.secret in {settings_path}")


def find_local_vsix(companion_file: str) -> str | None:
    """A repo checkout with a freshly built .vsix beats downloading."""
    ext_dir = os.path.join(os.path.dirname(os.path.abspath(companion_file)),
                           "..", "vscode-extension")
    candidates = sorted(glob.glob(os.path.join(ext_dir, "codelight-*.vsix")),
                        key=os.path.getmtime)
    return candidates[-1] if candidates else None


def install_vscode_extension(
    companion_file: str,
    secret: str = "",
    ws_port: int = 8765,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run=subprocess.run,
) -> None:
    """Install the codelight VSCode extension and configure settings."""
    found = find_vscode_cli(which=which)
    release_url = "https://github.com/henrikekblad/codelight/releases"
    if found is None:
        print("[vscode] 'code' CLI not found — install the extension manually:",
              file=sys.stderr)
        print(f"[vscode]   download codelight-*.vsix from {release_url}",
              file=sys.stderr)
        print("[vscode]   then: code --install-extension <file.vsix>",
              file=sys.stderr)
        return
    code, settings_path = found

    vsix_path = find_local_vsix(companion_file)
    if vsix_path:
        print(f"[vscode] using local build {os.path.basename(vsix_path)}")
    else:
        try:
            api = "https://api.github.com/repos/henrikekblad/codelight/releases/latest"
            with urllib.request.urlopen(api, timeout=15) as r:
                release = json.load(r)
            asset = next((a for a in release.get("assets", [])
                          if a.get("name", "").endswith(".vsix")), None)
            if asset is None:
                print(f"[vscode] no .vsix asset in the latest release — see {release_url}",
                      file=sys.stderr)
                return
            cache = os.path.expanduser("~/.cache/codelight")
            os.makedirs(cache, exist_ok=True)
            vsix_path = os.path.join(cache, asset["name"])
            print(f"[vscode] downloading {asset['name']}…")
            urllib.request.urlretrieve(asset["browser_download_url"], vsix_path)
        except Exception as e:
            print(f"[vscode] could not download extension: {e}", file=sys.stderr)
            return

    try:
        result = run([code, "--install-extension", vsix_path, "--force"],
                     capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[vscode] install failed: {result.stderr.strip()}",
                  file=sys.stderr)
            return
        print(f"[vscode] extension installed ({os.path.basename(vsix_path)})")
    except Exception as e:
        print(f"[vscode] could not install extension: {e}", file=sys.stderr)
        return

    if secret:
        configure_vscode_settings(settings_path, secret, ws_port)


def uninstall_vscode_extension(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run=subprocess.run,
) -> None:
    """Remove the extension and its settings from every VSCode flavor present."""
    for cli, settings in VSCODE_FLAVORS:
        exe = which(cli)
        if not exe:
            continue
        try:
            listed = run([exe, "--list-extensions"],
                         capture_output=True, text=True)
            if VSCODE_EXT_ID in listed.stdout:
                run([exe, "--uninstall-extension", VSCODE_EXT_ID],
                    capture_output=True, text=True)
                print(f"[vscode] extension removed from {cli}")
        except Exception:
            pass

        settings_path = os.path.expanduser(settings)
        try:
            with open(settings_path) as f:
                data = json.load(f)
            cleaned = {k: v for k, v in data.items()
                       if not k.startswith("codelight.")}
            if cleaned != data:
                with open(settings_path, "w") as f:
                    json.dump(cleaned, f, indent=4)
                    f.write("\n")
                print(f"[vscode] settings cleaned in {settings_path}")
        except Exception:
            pass
