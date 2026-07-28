#!/usr/bin/env python3
"""
main.py

Single entrypoint for the hound-orchestrator. Right now this rigs together:
  - `install`   -> installer.py: clone/build/verify every tool in tools.yaml
  - `check`     -> installer.py: verify only, no installing
  - `bh ...`    -> bloodhound_manager.py: the BloodHound instance gate
                   (setup-manual / auto / status / nuke)
  - `run`       -> the collection job runner (stateless execution for testing)
  - `update ...`-> update.py: check/update third-party tools, or check/update
                   IDKHound itself (self-check / self-update)
"""

import argparse
import json
import sys
from pathlib import Path

import bloodhound_manager as bhm
import installer
import manifest as mf
import runner
import update as upd

HOSTS_FILE = Path("/etc/hosts")


def _domain_in_hosts(domain: str) -> bool:
    """
    Cheap sanity check: is `domain` resolvable via /etc/hosts? Several
    collectors (bloodhound-py, ConfigManBearPig) do their own DNS lookup for
    the domain rather than only using --dc-ip directly, and blow up with a
    LifetimeTimeout if that lookup has nowhere to resolve (see TEMP for a
    real example). This doesn't touch DNS itself - it's just checking
    whether a hosts-file fallback exists.
    """
    if not HOSTS_FILE.exists():
        return False
    domain_lower = domain.lower()
    try:
        for line in HOSTS_FILE.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            if domain_lower in (p.lower() for p in parts[1:]):
                return True
    except OSError:
        return False
    return False


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
    elif args.bh_command == "auto":
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
    # self-check / self-update operate on the IDKHound repo itself, not on
    # tools.yaml, so they don't need a loaded manifest at all.
    if args.update_command == "self-check":
        local, remote, err = upd.self_check()
        if err:
            print(f"[!] IDKHound self-check failed: {err}")
            sys.exit(1)
        behind = local != remote
        status = "[*] update available" if behind else "[+] up to date"
        print(f"IDKHound   {local[:10]:<12} {remote[:10]:<12} {status}")
        sys.exit(1 if behind else 0)

    if args.update_command == "self-update":
        ok = upd.self_update(force=args.force)
        sys.exit(0 if ok else 1)

    manifest_data = mf.load_manifest()
    only = args.only.split(",") if args.only else None

    if args.update_command == "check":
        results = upd.check_all(manifest_data, only=only)
        upd.print_check(results)
        sys.exit(1 if any(st.error for st in results) else 0)

    elif args.update_command == "update":
        ok = upd.update_all(manifest_data, only=only, force=args.force)
        sys.exit(0 if ok else 1)


def _preflight_checks(manifest_data, domain=None, dc_ip=None,
                      username=None, password=None) -> bool:
    ok = True

    # BloodHound instance check is removed here so the stateless runner 
    # can execute and generate JSON/ZIPs without a live BloodHound connection.

    missing = [k for k in mf.all_tool_keys(manifest_data) if not mf.is_installed(manifest_data, k)]
    if missing:
        print(f"[!] The following tools are not installed: {', '.join(missing)}")
        print(f"    Run: python3 main.py install")
        ok = False
    else:
        print("[+] All manifest tools are installed.")

    if domain:
        if _domain_in_hosts(domain):
            print(f"[+] '{domain}' resolves via /etc/hosts.")
        else:
            print(f"[!] '{domain}' was not found in /etc/hosts - tools that resolve the "
                  f"domain themselves (rather than only using --dc-ip) may time out.")
            print(f"    Easiest fix: let NetExec build a hosts file from SMB enumeration,")
            print(f"    then append it to /etc/hosts:")
            print(f"")
            print(f"        nxc smb {dc_ip} -u '{username}' -p '{password}' --generate-hosts-file")
            print(f"        sudo tee -a /etc/hosts < hosts.txt")
            print(f"")
            print(f"    (or just add a single line for the DC: ")
            print(f"        echo '{dc_ip} {domain}' | sudo tee -a /etc/hosts)")
            ok = False

    return ok


def cmd_run(args):
    manifest_data = mf.load_manifest()
    if not _preflight_checks(
        manifest_data,
        domain=args.domain,
        dc_ip=args.dc_ip,
        username=args.username,
        password=args.password,
    ):
        print("\n[!] Preflight checks failed - fix the above before running collection jobs.")
        sys.exit(1)

    # Default to every tool in the manifest
    if args.tools:
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    else:
        tools = mf.all_tool_keys(manifest_data)

    # Validate requested tools exist in manifest
    unknown = [t for t in tools if t not in manifest_data.get("tools", {})]
    if unknown:
        print(f"[!] Unknown tool(s): {', '.join(unknown)}")
        print(f"    Available: {', '.join(mf.all_tool_keys(manifest_data))}")
        sys.exit(1)

    # Skip bloodhound-automation — it's a meta/upload tool, not a collector
    tools = [t for t in tools if manifest_data["tools"][t].get("command_template")]

    run_args = runner.RunArgs(
        domain=args.domain,
        dc_ip=args.dc_ip,
        username=args.username,
        password=args.password,
        tools=tools,
        data_dir=Path("data"),
        log_dir=Path("logs"),
    )

    ok = runner.run(run_args, manifest_data)
    sys.exit(0 if ok else 1)


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

    p_auto = bh_sub.add_parser("auto")
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

    p_run = sub.add_parser("run", help="Run collection jobs (stateless test runner)")
    p_run.add_argument("-d", "--domain", required=True, help="Target domain (e.g. corp.local)")
    p_run.add_argument("--dc-ip", required=True, help="Domain controller IP")
    p_run.add_argument("-u", "--username", required=True, help="Username for authentication")
    p_run.add_argument("-p", "--password", required=True, help="Password for authentication")
    p_run.add_argument("--tools", default=None, help="Comma-separated list of tools to run (default: all)")
    p_run.add_argument("--force", action="store_true", help="Ignored in stateless runner, kept for CLI compatibility")
    p_run.set_defaults(func=cmd_run)

    # NOTE: previously defined (cmd_update) but never actually registered as
    # a subcommand, so `update` was unreachable from the CLI - fixed here.
    p_update = sub.add_parser("update", help="Check/update tools, or check/update IDKHound itself")
    update_sub = p_update.add_subparsers(dest="update_command", required=True)

    p_upd_check = update_sub.add_parser("check", help="Compare local vs remote commits for all tools")
    p_upd_check.add_argument("--only", default=None, help="Comma-separated tool keys")

    p_upd_update = update_sub.add_parser("update", help="git pull + rebuild tools that are behind")
    p_upd_update.add_argument("--only", default=None, help="Comma-separated tool keys")
    p_upd_update.add_argument("--force", action="store_true", help="Rebuild even if already up to date")

    update_sub.add_parser("self-check", help="Check if IDKHound itself has updates available")

    p_upd_self_update = update_sub.add_parser(
        "self-update",
        help="Pull the latest IDKHound source (pure Python, no rebuild needed)",
    )
    p_upd_self_update.add_argument("--force", action="store_true", help="Pull even if already up to date")

    p_update.set_defaults(func=cmd_update)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()