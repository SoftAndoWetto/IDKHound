#!/usr/bin/env python3
"""
bloodhound_manager.py

Enforces a single precondition for the hound-orchestrator: no collector job
may run without a configured, reachable BloodHound instance. Supports two
setup paths, both writing to the same state file:

  1. manual   - point at an existing BloodHound instance you already run
  2. automate - spin one up via bloodhound-automation with a known
                (default or explicit) password

Also provides `nuke`, which forcibly tears down everything bloodhound-automate
created (containers, volumes, networks) directly via docker — NOT via
bloodhound-automation's own teardown path, since that tool being in a broken
state is exactly the scenario this exists for.

State file: state/bloodhound_instance.json
"""

import argparse
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import manifest as mf

try:
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.spinner import Spinner
    from rich.console import Group, Console
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
INSTANCE_FILE = STATE_DIR / "bloodhound_instance.json"

_manifest = mf.load_manifest()
BH_AUTOMATE_KEY = "bloodhound-automation"
BH_AUTOMATE_DIR = mf.tool_dir(_manifest, BH_AUTOMATE_KEY)
DEFAULT_BP = 10001   # bolt port
DEFAULT_NP = 10501   # neo4j port
DEFAULT_WP = 8001    # web port

# bloodhound-automation uses a static default password when --password isn't
# passed. Set this to match that real default before relying on it - if you
# always pass --password yourself, this constant never gets used.
DEFAULT_AUTOMATE_PASSWORD = "Password123!"

PROJECTS_DIR = BH_AUTOMATE_DIR / "projects"


# --------------------------------------------------------------------------- #
# Docker environment helpers
# --------------------------------------------------------------------------- #

# When True, all docker commands are wrapped in `sg docker -c '...'` because
# the current shell doesn't have the docker group active yet (the user was
# just added via usermod and hasn't logged out/in).  `sg` is the
# non-interactive cousin of `newgrp` - it runs a single command with the
# supplementary group active and returns, instead of spawning an interactive
# subshell that the script can't control.
_DOCKER_SG_WRAP = False


def _shlex_join(cmd: list) -> str:
    """shlex.join shim for Python < 3.8."""
    return " ".join(shlex.quote(str(c)) for c in cmd)


def _docker_cmd(*args) -> list:
    """Build a docker command list, transparently wrapping in ``sg docker -c``
    when the current shell lacks the docker group."""
    cmd = ["docker", *args]
    if _DOCKER_SG_WRAP:
        return ["sg", "docker", "-c", _shlex_join(cmd)]
    return cmd


def _run_sudo(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run *cmd* with sudo unless we're already root."""
    if os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    return subprocess.run(cmd, **kwargs)


def _docker_accessible() -> bool:
    """True if ``docker ps`` works in the current shell without sudo."""
    return subprocess.run(
        ["docker", "ps"], capture_output=True, text=True
    ).returncode == 0


def _docker_accessible_via_sg() -> bool:
    """True if ``docker ps`` works when wrapped in ``sg docker -c``."""
    try:
        return subprocess.run(
            ["sg", "docker", "-c", "docker ps"],
            capture_output=True, text=True,
        ).returncode == 0
    except FileNotFoundError:
        return False


def _kill_process_group(proc: subprocess.Popen):
    """Terminate a Popen process **and** its entire process group.

    Needed because when ``_DOCKER_SG_WRAP`` is True the Popen is an ``sg``
    process whose child (the real ``docker logs -f``) won't receive a
    plain ``proc.terminate()``.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait()


def ensure_docker_environment():
    """
    Ensures Docker and docker-compose are installed, the docker service is
    running, and the current user can access docker without sudo.

    Steps (only the ones actually needed are executed):

      1. ``apt-get install docker.io docker-compose``  — if missing
      2. ``systemctl start docker`` / ``enable docker``  — if not running
      3. ``usermod -aG docker $USER``                     — if not in group
      4. Verify access; if the group was just added and isn't active in the
         current shell, set ``_DOCKER_SG_WRAP`` so all subsequent docker
         commands (including the bloodhound-automation invocation) are
         transparently wrapped in ``sg docker -c '...'``.

    Exits the process with an error if docker cannot be made accessible.
    """
    global _DOCKER_SG_WRAP

    # ---- Fast path: docker already works ------------------------------- #
    if _docker_accessible():
        return

    # ---- Only automate installation on apt-based systems --------------- #
    if shutil.which("apt-get") is None:
        print("[!] Docker is not accessible and this system doesn't have apt-get.")
        print("    Please install Docker manually:")
        print("      https://docs.docker.com/engine/install/")
        print("    Then add yourself to the docker group:")
        print("      sudo usermod -aG docker $USER")
        print("    And log out and back in (or run 'newgrp docker').")
        sys.exit(1)

    # ---- 1. Install docker.io / docker-compose if missing -------------- #
    pkgs = []
    if shutil.which("docker") is None:
        pkgs.append("docker.io")
    if shutil.which("docker-compose") is None:
        # docker compose v2 plugin might already be bundled in docker.io,
        # but we can only check if docker itself is present.
        if shutil.which("docker") is not None:
            v2 = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True, text=True,
            )
            if v2.returncode != 0:
                pkgs.append("docker-compose")
        else:
            pkgs.append("docker-compose")

    if pkgs:
        print(f"[*] Installing: {', '.join(pkgs)} ...")
        r = _run_sudo(["apt-get", "update"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[!] apt-get update failed:\n{r.stderr}")
            sys.exit(1)
        r = _run_sudo(
            ["apt-get", "install", "-y", *pkgs],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"[!] apt-get install failed:\n{r.stderr}")
            sys.exit(1)
        print(f"[+] Installed: {', '.join(pkgs)}")

    # ---- 2. Start & enable the docker service -------------------------- #
    print("[*] Ensuring docker service is running ...")
    _run_sudo(["systemctl", "start", "docker"], capture_output=True, text=True)
    _run_sudo(["systemctl", "enable", "docker"], capture_output=True, text=True)
    status = _run_sudo(
        ["systemctl", "is-active", "docker"],
        capture_output=True, text=True,
    )
    if status.returncode != 0:
        print(f"[!] Docker service failed to start "
              f"(is-active: {status.stdout.strip()}).")
        print("    Check: sudo journalctl -u docker")
        sys.exit(1)
    print("[+] Docker service is running.")

    # ---- 3. Check access again ----------------------------------------- #
    if _docker_accessible():
        print("[+] Docker is accessible.")
        return

    # Running as root should already have access after the service starts.
    if os.geteuid() == 0:
        print("[!] Running as root but docker is still not accessible.")
        print("    Check the docker daemon logs: journalctl -u docker")
        sys.exit(1)

    # ---- 4. Add current user to the docker group ----------------------- #
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        print("[!] Could not determine current username.")
        sys.exit(1)

    groups_out = subprocess.run(["id", "-nG"], capture_output=True, text=True)
    user_groups = groups_out.stdout.split() if groups_out.returncode == 0 else []

    if "docker" not in user_groups:
        print(f"[*] Adding user '{user}' to the docker group ...")
        r = _run_sudo(
            ["usermod", "-aG", "docker", user],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"[!] usermod failed:\n{r.stderr}")
            sys.exit(1)
        print(f"[+] User '{user}' added to the docker group.")
    else:
        print("[*] User is already in the docker group, but the group "
              "isn't active in this shell.")

    # ---- 5. Try sg docker -c as a live workaround ---------------------- #
    # `sg` checks group membership from /etc/group, not from the current
    # process's supplementary groups, so it works immediately after usermod
    # without requiring a re-login.  `newgrp` would also work but opens an
    # interactive subshell we can't control from a script.
    if _docker_accessible_via_sg():
        print("[+] Docker is accessible via 'sg docker -c'.")
        print("[!] Your current shell doesn't have the docker group active.")
        print("    Commands will be wrapped in 'sg docker -c' as a workaround.")
        print("    For a permanent fix, log out and back in "
              "(or run 'newgrp docker').\n")
        _DOCKER_SG_WRAP = True
        return

    # ---- 6. Last resort ------------------------------------------------ #
    print("[!] Docker is installed and running, but the current user still")
    print("    cannot access it.  Please log out and back in (or run")
    print("    'newgrp docker'), then re-run this command.")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# State I/O
# --------------------------------------------------------------------------- #

def _now():
    return datetime.now(timezone.utc).isoformat()


def load_instance():
    if not INSTANCE_FILE.exists():
        return None
    try:
        return json.loads(INSTANCE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_instance(data: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    INSTANCE_FILE.write_text(json.dumps(data, indent=2))
    # Contains credentials in automate mode - lock it down.
    os.chmod(INSTANCE_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def clear_instance():
    if INSTANCE_FILE.exists():
        INSTANCE_FILE.unlink()


# --------------------------------------------------------------------------- #
# Precondition gate - call this before any collector job runs
# --------------------------------------------------------------------------- #

class BloodHoundNotReady(Exception):
    pass


def require_ready() -> dict:
    """
    Raises BloodHoundNotReady if no instance is configured. Call this at the
    very top of the orchestrator's run command, before the job matrix is
    built - not per-job. One gate, checked once, up front.
    """
    inst = load_instance()
    if inst is None:
        raise BloodHoundNotReady(
            "No BloodHound instance is configured. Set one up first:\n"
            "  bloodhound_manager.py setup-manual --url ... --user ... --password-env ...\n"
            "  bloodhound_manager.py auto <Name>\n"
        )
    return inst


# --------------------------------------------------------------------------- #
# Manual setup
# --------------------------------------------------------------------------- #

def setup_manual(url: str, username: str, password_env: str, neo4j_url: str = None):
    """
    Registers connection details for an existing/manually-run BloodHound
    instance. The password is stored as an env-var *reference*, matching the
    secret-handling pattern used everywhere else in the orchestrator config -
    never the raw value.
    """
    if password_env not in os.environ:
        print(f"[!] Warning: env var {password_env} is not currently set. "
              f"Export it before running collector/upload jobs.")

    save_instance({
        "mode": "manual",
        "url": url,
        "username": username,
        "password_env": password_env,
        "neo4j_url": neo4j_url,
    })
    print(f"[+] Stored manual BloodHound instance config at {INSTANCE_FILE}")


# --------------------------------------------------------------------------- #
# bloodhound-automate setup
# --------------------------------------------------------------------------- #

# Printed right before the success banner when start() finishes - used as a
# sanity check alongside the exit code, not as the primary success signal.
SUCCESS_BANNER_MARKER = "You are using BHCE"


def _run_with_live_view(cmd, cwd, title: str, project_name: str = None,
                         tail_lines: int = 14):
    """
    Runs `cmd` in `cwd` and shows a live panel while it's running.

    bloodhound-automation itself doesn't print incremental progress, so the
    live signal comes from `docker logs -f` on whatever containers it spins
    up. Discovery is done by diffing `docker ps -a -q` before/after
    starting `cmd`, rather than filtering by name - the tool's actual
    container-naming scheme isn't guaranteed to contain `project_name`, and
    a name-filter that silently matches nothing looks identical to
    "nothing is happening yet".

    Lifetime guarantee: every `docker logs -f` follower is tied to a
    stop_event that's set the instant `cmd`'s process exits, and each
    follower is terminate()'d right after - `docker logs -f` never keeps
    running on its own past that point.

    Returns (returncode, full_output_str).
    """
    baseline_check = subprocess.run(
        _docker_cmd("ps", "-a", "-q"), capture_output=True, text=True
    )
    if baseline_check.returncode != 0:
        baseline_ids = set()
        docker_error = baseline_check.stderr.strip() or "unknown error running `docker ps`"
    else:
        baseline_ids = set(
            line.strip() for line in baseline_check.stdout.splitlines() if line.strip()
        )
        docker_error = None

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    full_lines = []
    tail = deque(maxlen=tail_lines)
    start = time.monotonic()
    lock = threading.Lock()
    stop_event = threading.Event()
    seen_containers = set()
    log_threads = []

    def _append(entry: str):
        with lock:
            full_lines.append(entry)
            tail.append(entry)

    if docker_error:
        _append(f"[!] `docker ps` failed, container logs won't be available: {docker_error}")

    def _stream_container_logs(cid: str):
        name_res = subprocess.run(
            _docker_cmd("inspect", "--format", "{{.Name}}", cid),
            capture_output=True, text=True,
        )
        cname = name_res.stdout.strip().lstrip("/") or cid[:12]
        _append(f"[+] attached to container '{cname}'")
        try:
            log_proc = subprocess.Popen(
                _docker_cmd("logs", "-f", cid),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                start_new_session=True,  # so _kill_process_group can reach children
            )
        except FileNotFoundError as e:
            _append(f"[!] couldn't run `docker logs` for {cname}: {e}")
            return
        try:
            for line in log_proc.stdout:
                if stop_event.is_set():
                    break
                _append(f"[{cname}] {line.rstrip(chr(10))}")
        except Exception as e:
            _append(f"[!] log stream for {cname} errored: {e}")
        finally:
            _kill_process_group(log_proc)

    def _watch_containers():
        if docker_error:
            return
        while not stop_event.is_set():
            result = subprocess.run(
                _docker_cmd("ps", "-a", "-q"), capture_output=True, text=True
            )
            if result.returncode == 0:
                current_ids = set(
                    line.strip() for line in result.stdout.splitlines() if line.strip()
                )
                for cid in current_ids - baseline_ids:
                    if cid not in seen_containers:
                        seen_containers.add(cid)
                        t = threading.Thread(
                            target=_stream_container_logs, args=(cid,), daemon=True
                        )
                        t.start()
                        log_threads.append(t)
            stop_event.wait(1.0)

    watcher = threading.Thread(target=_watch_containers, daemon=True)
    watcher.start()

    if _HAS_RICH:
        _stream_with_rich(proc, title, full_lines, tail, start, lock)
    else:
        _stream_plain(proc, title, full_lines, tail, start, lock)

    stop_event.set()
    for t in log_threads:
        t.join(timeout=3)

    returncode = proc.wait()
    return returncode, "\n".join(full_lines)

def _stream_with_rich(proc, title, full_lines, tail, start, lock):
    def render():
        elapsed = time.monotonic() - start
        header = Spinner("dots", text=f" {title}  ({elapsed:0.1f}s elapsed)")
        with lock:
            body_text = "\n".join(tail) or "(waiting for container output...)"
        body = Text(body_text, style="dim")
        return Panel(Group(header, "", body), title="bloodhound-automation", border_style="cyan")

    with Live(render(), refresh_per_second=8, transient=False) as live:
        # Poll instead of a blocking `for line in proc.stdout` so container
        # log lines (appended from other threads) still get rendered even
        # while the main process itself is silent/buffering.
        while proc.poll() is None:
            live.update(render())
            time.sleep(0.125)
        # Drain anything left in the main process's own buffered stdout.
        for line in proc.stdout:
            with lock:
                full_lines.append(line.rstrip("\n"))
                tail.append(line.rstrip("\n"))
        live.update(render())

    rc = proc.poll()
    status = "[green]done[/green]" if rc == 0 else f"[red]exited {rc}[/red]"
    Console().print(f"[bold]{title}[/bold] - {status}")


def _stream_plain(proc, title, full_lines, tail, start, lock):
    spinner_frames = "|/-\\"
    frame_i = 0
    width = shutil.get_terminal_size(fallback=(80, 20)).columns
    printed = 0

    print(f"[*] {title}")
    while proc.poll() is None:
        with lock:
            new_lines = list(full_lines)[printed:]
            printed = len(full_lines)
        for line in new_lines:
            sys.stdout.write("\r" + " " * width + "\r")
            print(f"    {line}")

        elapsed = time.monotonic() - start
        spin = spinner_frames[frame_i % len(spinner_frames)]
        frame_i += 1
        status = f"[{spin}] running ({elapsed:0.1f}s)"
        sys.stdout.write("\r" + status[:width].ljust(width))
        sys.stdout.flush()
        time.sleep(0.125)

    with lock:
        new_lines = list(full_lines)[printed:]
    for line in new_lines:
        sys.stdout.write("\r" + " " * width + "\r")
        print(f"    {line}")

    sys.stdout.write("\r" + " " * width + "\r")
    rc = proc.poll()
    print(f"[*] {title} - {'done' if rc == 0 else f'exited {rc}'}")


def setup_automate(name: str, bp: int = DEFAULT_BP, np: int = DEFAULT_NP,
                    wp: int = DEFAULT_WP, password: str = None):
    """
    Runs `bloodhound-automation.py start -bp <bp> -np <np> -wp <wp>
    --password <password> <name>` and persists the instance details on
    success. Confirmation of success uses three signals, in order of trust:

      1. Exit code - subprocess.run() already blocks until the process
         exits, so by the time we get here start() has either fully
         finished or failed. No polling loop needed for that reason alone.
      2. The "You are using BHCE" banner line in stdout, as a sanity check.
      3. Existence of projects/<name>/project.pkl - the file save() writes
         as the very last step of start(), so its presence is the most
         concrete evidence the project is fully set up.

    Password handling: bloodhound-automation uses a static default password
    when none is passed. If `password` is given here, it's passed straight
    through and stored. If not, DEFAULT_AUTOMATE_PASSWORD is used and passed
    explicitly too - either way we always know the exact value being stored,
    with no need to parse it back out of anything.
    """

    # ---- Ensure Docker is installed, running, and accessible ----------- #
    ensure_docker_environment()

    if not mf.is_installed(_manifest, BH_AUTOMATE_KEY):
        print(f"[!] bloodhound-automation isn't installed. Run:\n"
              f"    python3 installer.py install --only {BH_AUTOMATE_KEY}")
        sys.exit(1)

    password_to_use = password if password else DEFAULT_AUTOMATE_PASSWORD

    cmd = mf.resolve_invocation(_manifest, BH_AUTOMATE_KEY) + [
        "start",
        "-bp", str(bp), "-np", str(np), "-wp", str(wp),
        "--password", password_to_use,
        name,
    ]

    # If the docker group isn't active in the current shell, wrap the entire
    # bloodhound-automation invocation in `sg docker -c '...'` so that all
    # docker commands it spawns internally inherit the docker group.
    if _DOCKER_SG_WRAP:
        cmd = ["sg", "docker", "-c", _shlex_join(cmd)]

    print(f"[*] Running: {' '.join(cmd)}")
    returncode, combined_output = _run_with_live_view(
        cmd, cwd=BH_AUTOMATE_DIR,
        title=f"Spinning up BloodHound instance '{name}'",
        project_name=name,
    )

    if returncode != 0:
        print(f"[!] bloodhound-automation exited with code {returncode}. "
              f"Instance was NOT marked ready.")
        sys.exit(returncode)

    if SUCCESS_BANNER_MARKER not in combined_output:
        print(f"[!] Exit code was 0 but the '{SUCCESS_BANNER_MARKER}' banner "
              f"line wasn't seen in output - proceeding, but double check "
              f"the output above.")

    project_pkl = PROJECTS_DIR / name / "project.pkl"
    if not project_pkl.exists():
        print(f"[!] {project_pkl} does not exist. start() reported success "
              f"but the project wasn't actually saved - treating this as a "
              f"failure. Check the output above.")
        sys.exit(1)

    save_instance({
        "mode": "automate",
        "project_name": name,
        "ports": {"bolt": bp, "neo4j": np, "web": wp},
        "username": "admin",  # confirm against real tool output/docs
        "password": password_to_use,
        "url": f"http://localhost:{wp}",
    })
    print(f"[+] BloodHound instance '{name}' is up. Credentials stored at {INSTANCE_FILE}")


# --------------------------------------------------------------------------- #
# Nuke - full teardown, independent of bloodhound-automation's own cleanup
# --------------------------------------------------------------------------- #

def nuke(name: str, assume_yes: bool = False):
    """
    Forcibly removes everything bloodhound-automate created for `name`:
    containers, volumes, networks - found and removed directly via docker,
    NOT via bloodhound-automation's own stop/teardown command. Deliberate:
    if bloodhound-automate is in a broken state, trusting its own cleanup
    path is exactly the failure mode this guards against.
    """
    if not assume_yes:
        confirm = input(
            f"This forcibly removes ALL docker containers, volumes, and "
            f"networks matching '{name}'. This cannot be undone.\n"
            f"Type the project name to confirm: "
        )
        if confirm != name:
            print("[!] Confirmation did not match. Aborting.")
            return

    print(f"[*] Nuking everything matching '{name}'...")

    # Docker Compose lowercases project names for containers/networks, 
    # so we check for both the exact name and the lowercase version to be safe.
    names_to_check = {name, name.lower()}
    
    ids = set()
    for n in names_to_check:
        ids.update(_docker_list("ps", "-a", "-q", "--filter", f"name={n}"))
    if ids:
        _docker_run("rm", "-f", *ids)
        print(f"[+] Removed {len(ids)} container(s).")
    else:
        print("[*] No matching containers found.")

    vols = set()
    for n in names_to_check:
        vols.update(_docker_list("volume", "ls", "-q", "--filter", f"name={n}"))
    if vols:
        _docker_run("volume", "rm", "-f", *vols)
        print(f"[+] Removed {len(vols)} volume(s).")
    else:
        print("[*] No matching volumes found.")

    nets = set()
    for n in names_to_check:
        nets.update(_docker_list("network", "ls", "-q", "--filter", f"name={n}"))
    if nets:
        _docker_run("network", "rm", *nets)
        print(f"[+] Removed {len(nets)} network(s).")
    else:
        print("[*] No matching networks found.")

    # Local project state (project.pkl etc.)
    project_dir = PROJECTS_DIR / name
    if project_dir.exists():
        import shutil
        shutil.rmtree(project_dir)
        print(f"[+] Removed local project directory {project_dir}.")
    else:
        print("[*] No local project directory found.")

    inst = load_instance()
    if inst and inst.get("mode") == "automate" and inst.get("project_name") == name:
        clear_instance()
        print("[+] Cleared stored instance config - setup-automate must be "
              "run again before any collector jobs can run.")

    print("[+] Nuke complete.")


def _docker_list(*args) -> list:
    result = subprocess.run(_docker_cmd(*args), capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _docker_run(*args):
    subprocess.run(_docker_cmd(*args), capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="BloodHound instance manager")
    sub = parser.add_subparsers(dest="command", required=True)

    p_manual = sub.add_parser("setup-manual", help="Register an existing/manual BloodHound instance")
    p_manual.add_argument("--url", required=True)
    p_manual.add_argument("--user", required=True)
    p_manual.add_argument("--password-env", required=True,
                           help="Name of the env var holding the password (never the raw value)")
    p_manual.add_argument("--neo4j-url", default=None)

    p_auto = sub.add_parser("auto", help="Spin up bloodhound-automate and register it")
    p_auto.add_argument("name")
    p_auto.add_argument("-bp", type=int, default=DEFAULT_BP, help=f"Bolt port (default {DEFAULT_BP})")
    p_auto.add_argument("-np", type=int, default=DEFAULT_NP, help=f"Neo4j port (default {DEFAULT_NP})")
    p_auto.add_argument("-wp", type=int, default=DEFAULT_WP, help=f"Web port (default {DEFAULT_WP})")
    p_auto.add_argument("--password", default=None,
                         help="Password to use (default: the tool's static default)")

    sub.add_parser("status", help="Show the current instance config")

    p_nuke = sub.add_parser("nuke", help="Forcibly tear down bloodhound-automate's containers/volumes/networks")
    p_nuke.add_argument("name")
    p_nuke.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    if args.command == "setup-manual":
        setup_manual(args.url, args.user, args.password_env, args.neo4j_url)
    elif args.command == "auto":
        setup_automate(args.name, args.bp, args.np, args.wp, args.password)
    elif args.command == "status":
        inst = load_instance()
        if inst is None:
            print("[!] No BloodHound instance configured.")
            sys.exit(1)
        print(json.dumps(inst, indent=2))
    elif args.command == "nuke":
        nuke(args.name, args.yes)


if __name__ == "__main__":
    main()