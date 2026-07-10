from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys


def build_args_line(
    *,
    name: str,
    secret: str,
    ws_port: int,
    verbose: bool,
    remote_control: bool = False,
    permission_timeout: int = 60,
    agents: set[str] | None = None,
    github_org: str = "",
    github_token_file: str = "",
) -> str:
    args_line = f"--name {shlex.quote(name)}"
    if secret:
        args_line += f" --secret {shlex.quote(secret)}"
    if ws_port != 8765:
        args_line += f" --ws-port {ws_port}"
    if verbose:
        args_line += " --verbose"
    if remote_control:
        args_line += " --remote-control"
        if permission_timeout != 60:
            args_line += f" --permission-timeout {permission_timeout}"
    enabled_agents = sorted(agents or set())
    if enabled_agents:
        args_line += f" --agents {','.join(enabled_agents)}"
    if github_org:
        args_line += f" --github-org {shlex.quote(github_org)}"
    if github_token_file:
        args_line += f" --github-token-file {shlex.quote(github_token_file)}"
    return args_line


def render_unit(*, python_path: str, script_path: str, args_line: str) -> str:
    return f"""\
[Unit]
Description=codelight coding-agent status monitor
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={python_path} -u {script_path} {args_line}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""


def install_service(
    *,
    name: str,
    secret: str,
    ws_port: int,
    verbose: bool,
    script_path: str,
    remote_control: bool = False,
    permission_timeout: int = 60,
    agents: set[str] | None = None,
    github_org: str = "",
    github_token_file: str = "",
    run=subprocess.run,
) -> None:
    """Write ~/.config/systemd/user/codelight.service and enable it."""
    python_path = shutil.which("python3") or "python3"
    args_line = build_args_line(
        name=name,
        secret=secret,
        ws_port=ws_port,
        verbose=verbose,
        remote_control=remote_control,
        permission_timeout=permission_timeout,
        agents=agents,
        github_org=github_org,
        github_token_file=github_token_file,
    )
    unit = render_unit(
        python_path=python_path,
        script_path=script_path,
        args_line=args_line,
    )

    service_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(service_dir, exist_ok=True)
    service_path = os.path.join(service_dir, "codelight.service")

    with open(service_path, "w") as f:
        f.write(unit)
    print(f"[install] wrote {service_path}")

    for cmd in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "reenable", "codelight"],
        ["systemctl", "--user", "restart", "codelight"],
    ]:
        result = run(cmd, capture_output=True, text=True)
        label = " ".join(cmd[2:])
        if result.returncode == 0:
            print(f"[install] systemctl {label}: ok")
        else:
            print(f"[install] systemctl {label}: {result.stderr.strip()}",
                  file=sys.stderr)

    print("[install] done — check status with: systemctl --user status codelight")


def uninstall_service(*, run=subprocess.run) -> None:
    service_path = os.path.expanduser("~/.config/systemd/user/codelight.service")
    if not os.path.exists(service_path):
        return
    run(["systemctl", "--user", "disable", "--now", "codelight"],
        capture_output=True)
    os.unlink(service_path)
    run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"[uninstall] removed {service_path}")
