from __future__ import annotations

import os
import shutil
import subprocess

from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent
from codelight_core import hooks as hooks_core
from codelight_core import service as service_core
from codelight_core import vscode as vscode_core


def detect_installed_agents() -> set[str]:
    return vscode_core.detect_installed_agents(
        which=shutil.which, run=subprocess.run)


def parse_agent_set(value: str | None, supported_agents: set[str]) -> set[str]:
    return vscode_core.parse_agent_set(value, supported_agents)


def install_vscode_extension(script_path: str, secret: str = "",
                             ws_port: int = 8765) -> None:
    vscode_core.install_vscode_extension(
        script_path, secret, ws_port, which=shutil.which, run=subprocess.run)


def uninstall_vscode_extension() -> None:
    vscode_core.uninstall_vscode_extension(
        which=shutil.which, run=subprocess.run)


def install_service(
    *,
    script_path: str,
    name: str,
    secret: str,
    ws_port: int,
    verbose: bool,
    remote_control: bool = False,
    permission_timeout: int = 60,
    agents: set[str] | None = None,
    github_org: str = "",
    github_token_file: str = "",
) -> None:
    service_core.install_service(
        name=name,
        secret=secret,
        ws_port=ws_port,
        verbose=verbose,
        script_path=script_path,
        remote_control=remote_control,
        permission_timeout=permission_timeout,
        agents=agents,
        github_org=github_org,
        github_token_file=github_token_file,
        run=subprocess.run,
    )


def uninstall(
    *,
    claude_settings_path: str,
    codex_home: str,
    copilot_home: str,
    policy_path: str,
    config_home: str,
    socket_path: str,
    monitor_state_dir: str,
) -> None:
    """Remove codelight hooks, local state, service, and optional clients."""
    hooks_core.remove_matcher_group_hooks(claude_settings_path)
    hooks_core.remove_matcher_group_hooks(codex_agent.hooks_path(codex_home))

    copilot_hooks = copilot_agent.hooks_path(copilot_home)
    service_core.remove_file(copilot_hooks)
    service_core.remove_empty_dir(os.path.dirname(copilot_hooks))

    service_core.remove_file(policy_path)
    service_core.remove_empty_dir(config_home)

    for path in [socket_path, monitor_state_dir]:
        service_core.remove_path(path)

    service_core.uninstall_service(run=subprocess.run)
    uninstall_vscode_extension()

    print("[uninstall] done")
