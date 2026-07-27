#!/usr/bin/env python3
"""
runner.py

Stateless job runner. Takes inline flags (domain, user, pass, dc, tools)
from main.py, builds the command strings, prints them to the terminal, 
and executes them sequentially so you can verify invocations visually.
"""

import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import manifest as mf

ROOT = Path(__file__).resolve().parent

# Constant wait between tools
DELAY_SECONDS = 5
# Constant timeout for all tools
DEFAULT_TIMEOUT_SECONDS = 600

class ConfigError(Exception):
    pass

@dataclass
class RunArgs:
    domain: str
    dc_ip: str
    username: str
    password: str
    tools: list
    data_dir: Path = ROOT / "data"
    log_dir: Path = ROOT / "logs"


def render_command(manifest_data: dict, tool_key: str, args: RunArgs) -> list:
    tool = manifest_data["tools"][tool_key]
    template = tool.get("command_template")
    if not template:
        raise ConfigError(f"tool '{tool_key}' has no command_template in tools.yaml")

    output_dir = args.data_dir / args.domain / args.username
    output_dir.mkdir(parents=True, exist_ok=True)

    subs = {
        "{domain}": args.domain,
        "{username}": args.username,
        "{password}": args.password,
        "{dc_ip}": args.dc_ip,
        "{output_dir}": str(output_dir),
    }

    rendered_tokens = []
    for tok in shlex.split(template):
        for placeholder, value in subs.items():
            if placeholder in tok:
                tok = tok.replace(placeholder, value)
        rendered_tokens.append(tok)

    prefix = mf.resolve_invocation(manifest_data, tool_key)
    return prefix + rendered_tokens


def run(args: RunArgs, manifest_data: dict) -> bool:
    print(f"\n[*] {len(args.tools)} job(s) queued.\n")

    all_ok = True
    for i, tool_key in enumerate(args.tools):
        print(f"[*] Running {tool_key} for {args.domain} / {args.username}...")
        
        try:
            argv = render_command(manifest_data, tool_key, args)
            print(f"    [>] Command: {' '.join(shlex.quote(a) for a in argv)}\n")
            print("    " + "-" * 60)
        except ConfigError as e:
            print(f"    [!] {tool_key}: {e}")
            all_ok = False
            continue

        try:
            # Executing directly without capturing stdout so output streams live to the terminal
            result = subprocess.run(argv, timeout=DEFAULT_TIMEOUT_SECONDS)
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            print(f"\n    [!] {tool_key}: failed - exceeded {DEFAULT_TIMEOUT_SECONDS}s timeout")
            all_ok = False
            if i < len(args.tools) - 1 and DELAY_SECONDS > 0:
                print(f"\n[*] Waiting {DELAY_SECONDS}s before next tool...\n")
                time.sleep(DELAY_SECONDS)
            continue
        except Exception as e:
            print(f"\n    [!] {tool_key}: failed to launch - {e}")
            all_ok = False
            if i < len(args.tools) - 1 and DELAY_SECONDS > 0:
                print(f"\n[*] Waiting {DELAY_SECONDS}s before next tool...\n")
                time.sleep(DELAY_SECONDS)
            continue

        print("    " + "-" * 60)
        if exit_code == 0:
            print(f"    [+] {tool_key}: done (exit code 0)")
        else:
            print(f"    [!] {tool_key}: failed (exit code {exit_code})")
            all_ok = False

        if i < len(args.tools) - 1 and DELAY_SECONDS > 0:
            print(f"\n[*] Waiting {DELAY_SECONDS}s before next tool...\n")
            time.sleep(DELAY_SECONDS)

    print("\n" + "=" * 74)
    print("RUN SUMMARY")
    print("=" * 74)
    for tool_key in args.tools:
        print(f"  {tool_key:<18} executed")
    print("=" * 74)
    
    return all_ok
