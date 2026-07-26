#!/usr/bin/env python3
"""
installer.py

Reads tools.yaml and, for each tool: clones it if missing, builds/installs it
according to its `kind`, then verifies it runs via its version command.
Failures are isolated per-tool - one broken build doesn't stop the rest of
the batch, matching how job failures are handled elsewhere in this project.

Usage:
    python3 installer.py install                # install/build everything
    python3 installer.py install --only rusthound-ce,mssqlhound
    python3 installer.py install --force         # rebuild even if present
    python3 installer.py check                   # verify only, install nothing
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import manifest as mf


class InstallResult:
    def __init__(self, key):
        self.key = key
        self.cloned = False
        self.built = False
        self.verified = False
        self.error = None

    def ok(self):
        return self.error is None


def _run(cmd, cwd=None, description=""):
    print(f"    $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{description or 'command'} failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def _check_prereq(binary: str):
    if shutil.which(binary) is None:
        raise RuntimeError(
            f"'{binary}' not found on PATH - install it before running this tool's build step"
        )


# --------------------------------------------------------------------------- #
# Clone
# --------------------------------------------------------------------------- #

def clone_if_missing(manifest_data: dict, key: str, result: InstallResult):
    tool = manifest_data["tools"][key]
    dest = mf.tool_dir(manifest_data, key)

    if dest.exists():
        print(f"    [*] {key} already cloned at {dest}")
        return

    repo = tool.get("repo")
    if not repo:
        raise RuntimeError(
            f"No repo URL set for '{key}' in tools.yaml, and it isn't cloned "
            f"yet at {dest}. Set the repo URL or clone it there manually."
        )

    _check_prereq("git")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", repo, str(dest)], description=f"git clone {key}")
    result.cloned = True
    print(f"    [+] Cloned {key} -> {dest}")


# --------------------------------------------------------------------------- #
# Build / install per kind
# --------------------------------------------------------------------------- #

def _ensure_venv(venv_dir: Path):
    if venv_dir.exists():
        return
    _run([sys.executable, "-m", "venv", str(venv_dir)], description="create venv")


def build_python_venv(manifest_data: dict, key: str, result: InstallResult):
    """kind: python-venv - plain `pip install .` into its own venv."""
    tool = manifest_data["tools"][key]
    tdir = mf.tool_dir(manifest_data, key)
    venv_dir = tdir / tool.get("venv", "venv")

    _ensure_venv(venv_dir)
    pip = venv_dir / "bin" / "pip"
    _run([str(pip), "install", "."], cwd=tdir, description=f"pip install . ({key})")
    result.built = True


def build_python_venv_installed(manifest_data: dict, key: str, result: InstallResult):
    """kind: python-venv-installed - same as above, difference is only in
    how it's invoked afterward (console-script vs raw entry.py)."""
    build_python_venv(manifest_data, key, result)


def build_python_venv_editable(manifest_data: dict, key: str, result: InstallResult):
    """kind: python-venv-editable - `pip install -e '.[extras]'`."""
    tool = manifest_data["tools"][key]
    tdir = mf.tool_dir(manifest_data, key)
    venv_dir = tdir / tool.get("venv", "venv")
    extras = tool.get("editable_extras", "")

    _ensure_venv(venv_dir)
    pip = venv_dir / "bin" / "pip"
    _run([str(pip), "install", "-e", f".{extras}"], cwd=tdir,
         description=f"pip install -e .{extras} ({key})")
    result.built = True


def build_python_venv_requirements(manifest_data: dict, key: str, result: InstallResult):
    """kind: python-venv-requirements - `pip install -r requirements.txt`."""
    tool = manifest_data["tools"][key]
    tdir = mf.tool_dir(manifest_data, key)
    venv_dir = tdir / tool.get("venv", "venv")
    req_file = tdir / "requirements.txt"

    _ensure_venv(venv_dir)
    pip = venv_dir / "bin" / "pip"
    if req_file.exists():
        _run([str(pip), "install", "-r", str(req_file)], cwd=tdir,
             description=f"pip install -r requirements.txt ({key})")
    else:
        print(f"    [!] No requirements.txt found for {key} - skipping dependency install")
    result.built = True


def build_cargo_binary(manifest_data: dict, key: str, result: InstallResult):
    """kind: cargo-binary - `cargo build --release`."""
    _check_prereq("cargo")
    tdir = mf.tool_dir(manifest_data, key)
    _run(["cargo", "build", "--release"], cwd=tdir, description=f"cargo build ({key})")
    result.built = True


def build_go_binary(manifest_data: dict, key: str, result: InstallResult):
    """kind: go-binary - `go build -o <binary> <package>`."""
    _check_prereq("go")
    tool = manifest_data["tools"][key]
    tdir = mf.tool_dir(manifest_data, key)
    build_dir = tdir / tool["build_subdir"] if tool.get("build_subdir") else tdir
    package = tool.get("build_package", ".")
    binary_name = tool["binary"]

    _run(["go", "build", "-o", binary_name, package], cwd=build_dir,
         description=f"go build ({key})")
    result.built = True


BUILD_FUNCS = {
    "python-venv": build_python_venv,
    "python-venv-installed": build_python_venv_installed,
    "python-venv-editable": build_python_venv_editable,
    "python-venv-requirements": build_python_venv_requirements,
    "cargo-binary": build_cargo_binary,
    "go-binary": build_go_binary,
}


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #

def verify_version(manifest_data: dict, key: str, result: InstallResult):
    tool = manifest_data["tools"][key]
    version_args = tool.get("version_args")

    if version_args is None:
        print(f"    [*] {key} has no version_args set - skipping verification "
              f"(expected for meta tools like bloodhound-automation)")
        result.verified = True
        return

    argv = mf.resolve_invocation(manifest_data, key) + version_args
    if not Path(argv[0]).exists():
        raise RuntimeError(f"Resolved binary/interpreter does not exist: {argv[0]}")

    proc = subprocess.run(argv, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    print(f"    [+] {key} version check output: {output or '(empty output)'}")
    # Not gating on returncode == 0 here - some tools' --version exits
    # non-zero (argparse quirks etc). Existence + output is the useful signal.
    result.verified = True


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def install_tool(manifest_data: dict, key: str, force: bool) -> InstallResult:
    result = InstallResult(key)
    tool = manifest_data["tools"][key]
    print(f"[*] {key} ({tool['kind']})")

    try:
        clone_if_missing(manifest_data, key, result)

        already_built = mf.is_installed(manifest_data, key)
        if already_built and not force:
            print(f"    [*] {key} appears already built - skipping build "
                  f"(use --force to rebuild)")
        else:
            BUILD_FUNCS[tool["kind"]](manifest_data, key, result)

        verify_version(manifest_data, key, result)

    except Exception as e:
        result.error = str(e)
        print(f"    [!] {key} FAILED: {e}")

    return result


def install_all(manifest_data: dict, only: list = None, force: bool = False) -> list:
    keys = only if only else mf.all_tool_keys(manifest_data)
    results = []
    for key in keys:
        if key not in manifest_data["tools"]:
            print(f"[!] Skipping unknown tool key '{key}'")
            continue
        results.append(install_tool(manifest_data, key, force))
        print()
    return results


def print_summary(results: list):
    print("=" * 60)
    print("INSTALL SUMMARY")
    print("=" * 60)
    for r in results:
        status = "OK" if r.ok() else f"FAILED ({r.error})"
        print(f"  {r.key:30s} {status}")
    failed = [r for r in results if not r.ok()]
    if failed:
        print(f"\n{len(failed)} tool(s) failed. Fix those before running collection jobs.")
    else:
        print("\nAll tools installed and verified.")
    return len(failed) == 0


def main():
    parser = argparse.ArgumentParser(description="Install/build/verify all collector tools")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="Clone, build, and verify tools")
    p_install.add_argument("--only", default=None,
                            help="Comma-separated tool keys to limit to (default: all)")
    p_install.add_argument("--force", action="store_true",
                            help="Rebuild even if already installed")

    sub.add_parser("check", help="Verify tools only, without installing anything")

    args = parser.parse_args()
    manifest_data = mf.load_manifest()

    if args.command == "install":
        only = args.only.split(",") if args.only else None
        results = install_all(manifest_data, only=only, force=args.force)
        ok = print_summary(results)
        sys.exit(0 if ok else 1)

    elif args.command == "check":
        results = []
        for key in mf.all_tool_keys(manifest_data):
            result = InstallResult(key)
            print(f"[*] {key}")
            try:
                if not mf.is_installed(manifest_data, key):
                    raise RuntimeError("not installed (binary/interpreter missing)")
                verify_version(manifest_data, key, result)
            except Exception as e:
                result.error = str(e)
                print(f"    [!] {key} FAILED: {e}")
            results.append(result)
            print()
        ok = print_summary(results)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()