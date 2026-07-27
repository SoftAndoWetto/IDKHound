#!/usr/bin/env python3
"""
main.py

Single entrypoint for the hound-orchestrator. Right now this rigs together:
  - `install`   -> installer.py: clone/build/verify every tool in tools.yaml
  - `check`     -> installer.py: verify only, no installing
  - `bh ...`    -> bloodhound_manager.py: the BloodHound instance gate
                   (setup-manual / setup-automate / status / nuke)
  - `run`       -> the collection job matrix (NOT YET BUILT - stubbed below,
                   this is the next piece)
"""

import argparse
import json
import sys

import bloodhound_manager as bhm
import installer
import manifest as mf
import update as upd


def cmd_install(args):
    manifest_data = mf.load_manifest()
    only = args.only.split(",") if args.only else None
    results = installer.install_all(manifest_data, only=only, force=args.force)
    ok = installer.print_summary(results)
    sys.exit(0 if ok else 1)


def cmd_check(args):
    manifest_data = mf.load_manifest()
    results = []
    for key in mf.all_tool_keys(manifest_data):
        result = installer.InstallResult(key)
        try:
            if not mf.is_installed(manifest_data, key):
                raise RuntimeError("not installed")
            installer.verify_version(manifest_data, key, result)
        except Exception as e:
            result.error = str(e)
            result.ok = False
        results.append(result)
    ok = installer.print_summary(results)
    sys.exit(0 if ok else 1)


def cmd_bh(args):
    if args.bh_command == "setup-manual":
        bhm.setup_manual(args.url, args.user, args.password_env, args.neo4j_url)
    elif args.bh_command == "setup-automate":
        bhm.setup_automate(args.name, args.bp, args.np, args.wp, args.password)
    elif args.bh_command == "status":
        inst = bhm.load_instance()
        if inst is None:
            print("[!] No BloodHound instance configured.")
            sys.exit(1)
        print(json.dumps(inst, indent=2))
    elif args.bh_command == "nuke":
        bhm.nuke(args.name, args.yes)


def cmd_update(args):
    manifest_data = mf.load_manifest()
    only = args.only.split(",") if args.only else None

    if args.update_command == "check":
        results = upd.check_all(manifest_data, only=only)
        upd.print_check(results)
        sys.exit(1 if any(st.error for st in results) else 0)

    elif args.update_command == "update":
        ok = upd.update_all(manifest_data, only=only, force=args.force)
        sys.exit(0 if ok else 1)


def _preflight_checks(manifest_data) -> bool:
    ok = True

    try:
        inst = bhm.require_ready()
        print(f"[+] BloodHound instance ready (mode: {inst['mode']})")
    except bhm.BloodHoundNotReady as e:
        print(f"[!] {e}")
        ok = False

    missing = [k for k in mf.all_tool_keys(manifest_data) if not mf.is_installed(manifest_data, k)]
    if missing:
        print(f"[!] The following tools are not installed: {', '.join(missing)}")
        print(f"    Run: python3 main.py install")
        ok = False
    else:
        print("[+] All manifest tools are installed.")

    return ok


def cmd_run(args):
    manifest_data = mf.load_manifest()
    if not _preflight_checks(manifest_data):
        print("\n[!] Preflight checks failed - fix the above before running collection jobs.")
        sys.exit(1)

    print("\n[*] Preflight checks passed. Job matrix / runner is not built yet.")


def build_parser():
    parser = argparse.ArgumentParser(description="hound-orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="Clone, build, and verify all tools")
    p_install.add_argument("--only", default=None, help="Comma-separated tool keys")
    p_install.add_argument("--force", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_check = sub.add_parser("check", help="Verify tools only, install nothing")
    p_check.set_defaults(func=cmd_check)

    p_bh = sub.add_parser("bh", help="Manage the BloodHound instance precondition")
    bh_sub = p_bh.add_subparsers(dest="bh_command", required=True)

    p_manual = bh_sub.add_parser("setup-manual")
    p_manual.add_argument("--url", required=True)
    p_manual.add_argument("--user", required=True)
    p_manual.add_argument("--password-env", required=True)
    p_manual.add_argument("--neo4j-url", default=None)

    p_auto = bh_sub.add_parser("setup-automate")
    p_auto.add_argument("name")
    p_auto.add_argument("-bp", type=int, default=bhm.DEFAULT_BP)
    p_auto.add_argument("-np", type=int, default=bhm.DEFAULT_NP)
    p_auto.add_argument("-wp", type=int, default=bhm.DEFAULT_WP)
    p_auto.add_argument("--password", default=None)

    bh_sub.add_parser("status")

    p_nuke = bh_sub.add_parser("nuke")
    p_nuke.add_argument("name")
    p_nuke.add_argument("-y", "--yes", action="store_true")

    p_bh.set_defaults(func=cmd_bh)

    p_update = sub.add_parser("update", help="Check tool versions by commit hash, and update+rebuild them")
    update_sub = p_update.add_subparsers(dest="update_command", required=True)

    p_upd_check = update_sub.add_parser("check", help="Compare local HEAD vs remote HEAD for all tools")
    p_upd_check.add_argument("--only", default=None, help="Comma-separated tool keys")

    p_upd_run = update_sub.add_parser("update", help="git pull + rebuild/reinstall tools that are behind")
    p_upd_run.add_argument("--only", default=None, help="Comma-separated tool keys")
    p_upd_run.add_argument("--force", action="store_true", help="Rebuild even if already up to date")

    p_update.set_defaults(func=cmd_update)

    p_run = sub.add_parser("run", help="Run collection jobs (preflight-checked)")
    p_run.set_defaults(func=cmd_run)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()