#!/usr/bin/env python3
"""
update.py

Two operations, nothing tool-specific hardcoded here:

  check  - compares each tool's local git HEAD against whatever commit HEAD
           currently points to at its repo URL (via `git ls-remote` - no
           local fetch, no mutation, works even if the tool isn't cloned
           yet).
  update - for any tool that's behind (or --force), runs `git pull` and
           then re-runs installer.install_tool() - the exact same
           clone/build/setup function used on first install. Pulling new
           source and rebuilding are never separate steps here, so an
           "updated" tool is always a freshly rebuilt, working binary/venv,
           never just newer source sitting on disk unbuilt.

installer.py's install_tool() is the single source of truth for how to
build each `kind` (including reading a tool's own kind-specific build
override, e.g. sharehound's non-default go build command, straight out of
tools.yaml) - this file doesn't duplicate any of that.
"""

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import installer
import manifest as mf


# --------------------------------------------------------------------------- #
# Git plumbing
# --------------------------------------------------------------------------- #

class GitError(Exception):
    pass


def _git(args, cwd=None, timeout=30) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise GitError(res.stderr.strip() or f"git {' '.join(args)} failed (exit {res.returncode})")
    return res.stdout.strip()


def local_commit(tool_path: Path) -> str:
    """Full SHA of the checked-out HEAD in an on-disk clone."""
    if not (tool_path / ".git").exists():
        raise GitError(f"{tool_path} is not a git repo (no .git dir)")
    return _git(["rev-parse", "HEAD"], cwd=tool_path)


def remote_commit(repo_url: str) -> str:
    """
    Full SHA of whatever HEAD currently points to at `repo_url` - fetched
    directly via `git ls-remote`, so this works even for a tool that isn't
    cloned yet and never touches a local working copy.
    """
    out = _git(["ls-remote", repo_url, "HEAD"], timeout=30)
    if not out:
        raise GitError(f"empty ls-remote response for {repo_url}")
    return out.split()[0]  # format: "<sha>\tHEAD"


# --------------------------------------------------------------------------- #
# Check
# --------------------------------------------------------------------------- #

@dataclass
class ToolStatus:
    tool: str
    installed: bool = False
    local: str = ""
    remote: str = ""
    behind: bool = False
    error: str = ""


def check_tool(manifest_data: dict, key: str) -> ToolStatus:
    st = ToolStatus(tool=key)
    tool = manifest_data["tools"][key]
    repo_url = tool.get("repo")
    tool_path = mf.tool_dir(manifest_data, key)

    if not repo_url:
        st.error = "no 'repo' field in manifest"
        return st

    try:
        st.remote = remote_commit(repo_url)
    except GitError as e:
        st.error = f"remote check failed: {e}"

    if not tool_path.exists():
        st.installed = False
        if not st.error:
            st.error = "not installed yet"
        return st

    st.installed = True
    try:
        st.local = local_commit(tool_path)
    except GitError as e:
        st.error = (st.error + "; " if st.error else "") + f"local check failed: {e}"
        return st

    if st.remote:
        st.behind = st.local != st.remote

    return st


def check_all(manifest_data: dict, only=None) -> list:
    keys = [k for k in mf.all_tool_keys(manifest_data) if (not only or k in only)]
    return [check_tool(manifest_data, k) for k in keys]


# --------------------------------------------------------------------------- #
# Update - pull, then always rebuild via installer.install_tool
# --------------------------------------------------------------------------- #

def update_tool(manifest_data: dict, key: str, force: bool = False) -> bool:
    tool_path = mf.tool_dir(manifest_data, key)
    if not tool_path.exists():
        print(f"[!] {key}: not installed yet - run `main.py install --only {key}` first.")
        return False

    st = check_tool(manifest_data, key)
    if st.error and not st.local:
        print(f"[!] {key}: {st.error}")
        return False

    if not force and not st.behind:
        print(f"[*] {key}: already up to date ({st.local[:10]}).")
        return True

    print(f"[*] {key}: pulling latest...")
    try:
        _git(["pull"], cwd=tool_path)
    except GitError as e:
        print(f"[!] {key}: git pull failed - {e}")
        return False

    print(f"[*] {key}: rebuilding/reinstalling...")
    ok, err = installer.install_tool(manifest_data, key)
    if not ok:
        print(f"[!] {key}: rebuild failed - {err}")
        return False

    try:
        new_commit = local_commit(tool_path)
        print(f"[+] {key}: updated and reinstalled, now at {new_commit[:10]}.")
    except GitError:
        print(f"[+] {key}: updated and reinstalled.")
    return True


def update_all(manifest_data: dict, only=None, force: bool = False) -> bool:
    keys = [k for k in mf.all_tool_keys(manifest_data) if (not only or k in only)]
    all_ok = True
    for key in keys:
        if not update_tool(manifest_data, key, force=force):
            all_ok = False
    return all_ok


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _fmt(sha: str) -> str:
    return sha[:10] if sha else "-"


def print_check(results: list):
    print("\n" + "=" * 62)
    print(f"{'TOOL':<25} {'LOCAL':<12} {'REMOTE':<12} STATUS")
    print("=" * 62)
    for st in results:
        if st.error:
            status = f"[!] {st.error}"
        elif st.behind:
            status = "[*] update available"
        else:
            status = "[+] up to date"
        print(f"{st.tool:<25} {_fmt(st.local):<12} {_fmt(st.remote):<12} {status}")
    print("=" * 62)


def main():
    parser = argparse.ArgumentParser(description="Check tool versions by commit hash and update+rebuild in place")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Compare local HEAD vs remote HEAD for all tools")
    p_check.add_argument("--only", default=None, help="Comma-separated tool keys")

    p_update = sub.add_parser("update", help="git pull + rebuild/reinstall tools that are behind")
    p_update.add_argument("--only", default=None, help="Comma-separated tool keys")
    p_update.add_argument("--force", action="store_true", help="Rebuild even if already up to date")

    args = parser.parse_args()
    manifest_data = mf.load_manifest()

    if args.command == "check":
        only = args.only.split(",") if args.only else None
        results = check_all(manifest_data, only=only)
        print_check(results)
        sys.exit(1 if any(st.error for st in results) else 0)

    elif args.command == "update":
        only = args.only.split(",") if args.only else None
        ok = update_all(manifest_data, only=only, force=args.force)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()