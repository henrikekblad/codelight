from __future__ import annotations

import shutil
import subprocess

from codelight_core.agents.registry import AgentRegistry
from codelight_core import hooks as hooks_core
from codelight_core import service as service_core
from codelight_core import vscode as vscode_core


def detect_installed_agents(agent_registry: AgentRegistry) -> set[str]:
    return vscode_core.detect_installed_agents(
        agent_executables=agent_registry.executables_by_agent(),
        agent_vscode_extensions=agent_registry.vscode_extensions_by_agent(),
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
        run=subprocess.run,
    )


def install_agent_hooks(
    *,
    agent_registry: AgentRegistry,
    enabled_agents: set[str],
    script_path: str,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    remote_questions: bool = False,
    permission_timeout: int = 60,
    log=None,
) -> None:
    agent_registry.install_hooks(
        enabled_agents=enabled_agents,
        script_path=script_path,
        hook_wait_ceiling=hook_wait_ceiling,
        remote_permissions=remote_permissions,
        remote_questions=remote_questions,
        permission_timeout=permission_timeout,
        log=log,
    )


def uninstall(
    *,
    agent_registry: AgentRegistry,
    policy_path: str,
    config_home: str,
    socket_path: str,
    monitor_state_dir: str,
) -> None:
    """Remove codelight hooks, local state, service, and optional clients."""
    for path in agent_registry.removable_hook_paths():
        hooks_core.remove_matcher_group_hooks(path)

    for path in agent_registry.removable_files():
        service_core.remove_file(path)
    for path in agent_registry.removable_empty_dirs():
        service_core.remove_empty_dir(path)

    service_core.remove_file(policy_path)
    service_core.remove_empty_dir(config_home)

    for path in [socket_path, monitor_state_dir]:
        service_core.remove_path(path)

    service_core.uninstall_service(run=subprocess.run)
    uninstall_vscode_extension()

    print("[uninstall] done")
