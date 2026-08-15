from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time

from codelight_core import invocation

# launchd label / D-Bus well-known name — kept identical so the service is
# recognisable under the same identifier on both platforms.
LAUNCHD_LABEL = "se.sensnology.codelight"


def build_args_line(
    *,
    name: str,
    secret: str,
    ws_port: int,
    verbose: bool,
    remote_control: bool = False,
    permission_timeout: int = 60,
    agents: set[str] | None = None,
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


def launch_agent_path() -> str:
    return os.path.expanduser(
        f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")


def launchd_log_path() -> str:
    return os.path.expanduser("~/Library/Logs/codelight.log")


def render_launch_agent(
    *,
    python_path: str,
    script_path: str,
    args_line: str,
    path_env: str,
    log_path: str,
) -> bytes:
    """Render the macOS LaunchAgent equivalent of :func:`render_unit`.

    ``KeepAlive{SuccessfulExit: False}`` is the launchd spelling of systemd's
    ``Restart=on-failure``: relaunch after a crash, but stay down when the user
    stops it deliberately.

    ``PATH`` is captured from the installing shell because launchd hands agents
    a minimal ``/usr/bin:/bin:/usr/sbin:/sbin``. Without it, agent detection
    (``shutil.which("claude")``) finds nothing and the daemon starts with no
    agents enabled.
    """
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python_path, "-u", script_path,
                             *shlex.split(args_line)],
        "EnvironmentVariables": {"PATH": path_env},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(plist)


BOOTSTRAP_ATTEMPTS = 5
BOOTSTRAP_RETRY_SECS = 0.5


def _run_until_ok(run, cmd, *, retry: bool, sleep=time.sleep):
    attempts = BOOTSTRAP_ATTEMPTS if retry else 1
    for attempt in range(attempts):
        result = run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        if attempt + 1 < attempts:
            sleep(BOOTSTRAP_RETRY_SECS)
    return result


def _install_launch_agent(
    *,
    python_path: str,
    script_path: str,
    args_line: str,
    path_env: str,
    run,
    sleep=time.sleep,
) -> None:
    log_path = launchd_log_path()
    plist_path = launch_agent_path()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)

    with open(plist_path, "wb") as f:
        f.write(render_launch_agent(
            python_path=python_path,
            script_path=script_path,
            args_line=args_line,
            path_env=path_env,
            log_path=log_path,
        ))
    print(f"[install] wrote {plist_path}")

    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LAUNCHD_LABEL}"
    # Unload any previous copy first; failure just means it wasn't loaded.
    run(["launchctl", "bootout", target], capture_output=True, text=True)

    for cmd in [
        ["launchctl", "enable", target],
        ["launchctl", "bootstrap", domain, plist_path],
        ["launchctl", "kickstart", "-k", target],
    ]:
        # bootout returns before launchd has finished tearing the job down, so
        # a reinstall can hit "Bootstrap failed: 5: Input/output error" while
        # the old copy is still leaving the domain. Retry rather than leaving
        # the user with a booted-out, never-bootstrapped service.
        result = _run_until_ok(run, cmd, retry=cmd[1] == "bootstrap",
                               sleep=sleep)
        label = " ".join(cmd[1:2])
        if result.returncode == 0:
            print(f"[install] launchctl {label}: ok")
        else:
            print(f"[install] launchctl {label}: {result.stderr.strip()}",
                  file=sys.stderr)

    print(f"[install] logs: {log_path}")
    print(f"[install] done — check status with: launchctl print {target}")


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
    run=subprocess.run,
    platform: str = sys.platform,
    sleep=time.sleep,
) -> None:
    """Install codelight as a per-user service and start it.

    systemd user unit on Linux, launchd LaunchAgent on macOS.
    """
    python_path, _ = invocation.self_invocation()
    args_line = build_args_line(
        name=name,
        secret=secret,
        ws_port=ws_port,
        verbose=verbose,
        remote_control=remote_control,
        permission_timeout=permission_timeout,
        agents=agents,
    )

    if platform == "darwin":
        _install_launch_agent(
            python_path=python_path,
            script_path=script_path,
            args_line=args_line,
            path_env=os.environ.get("PATH", ""),
            run=run,
            sleep=sleep,
        )
        return

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


def uninstall_service(*, run=subprocess.run,
                      platform: str = sys.platform) -> None:
    if platform == "darwin":
        plist_path = launch_agent_path()
        if not os.path.exists(plist_path):
            return
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            capture_output=True)
        os.unlink(plist_path)
        print(f"[uninstall] removed {plist_path}")
        return

    service_path = os.path.expanduser("~/.config/systemd/user/codelight.service")
    if not os.path.exists(service_path):
        return
    run(["systemctl", "--user", "disable", "--now", "codelight"],
        capture_output=True)
    os.unlink(service_path)
    run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"[uninstall] removed {service_path}")


def remove_file(path: str, *, label: str = "uninstall") -> bool:
    try:
        os.unlink(path)
        print(f"[{label}] removed {path}")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[{label}] could not remove {path}: {e}", file=sys.stderr)
        return False


def remove_empty_dir(path: str, *, label: str = "uninstall") -> bool:
    try:
        os.rmdir(path)
        print(f"[{label}] removed empty {path}")
        return True
    except OSError:
        return False


def remove_path(path: str, *, label: str = "uninstall") -> bool:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        print(f"[{label}] removed {path}")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[{label}] could not remove {path}: {e}", file=sys.stderr)
        return False
