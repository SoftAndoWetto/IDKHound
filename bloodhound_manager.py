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
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import manifest as mf

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
            "  bloodhound_manager.py setup-automate <Name>\n"
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



    # Note need to add checks for 
    sudo apt install docker.io
    sudo apt install docker-compose
    sudo usermod -aG docker $USER
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo systemctl status docker
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
    print(f"[*] Running: {' '.join(cmd)}")

    result = subprocess.run(cmd, cwd=BH_AUTOMATE_DIR, capture_output=True, text=True)
    combined_output = result.stdout + "\n" + result.stderr
    print(combined_output)

    if result.returncode != 0:
        print(f"[!] bloodhound-automation exited with code {result.returncode}. "
              f"Instance was NOT marked ready.")
        sys.exit(result.returncode)

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
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _docker_run(*args):
    subprocess.run(["docker", *args], capture_output=True, text=True)


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

    p_auto = sub.add_parser("setup-automate", help="Spin up bloodhound-automate and register it")
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
    elif args.command == "setup-automate":
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
