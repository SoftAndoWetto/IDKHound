#!/usr/bin/env python3
"""
runner.py

Stateless job runner. Takes inline flags (domain, user, pass, dc, tools)
from main.py, builds the command strings, prints them to the terminal, 
and executes them sequentially so you can verify invocations visually.

Each tool's stdout/stderr streams through live_output.RollingPanel - a
title line plus an auto-updating window of the last few lines - rather
than dumping potentially thousands of raw lines straight to the terminal.
The full output is still captured internally, so a failure prints a full
tail for debugging even though the live view only showed a handful of
lines at a time.
"""

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import live_output as live
import manifest as mf

ROOT = Path(__file__).resolve().parent

# Constant wait between tools
DELAY_SECONDS = 5
# Constant timeout for all tools
DEFAULT_TIMEOUT_SECONDS = 600
# How many of the most recent output lines stay visible per tool
PANEL_LINES = 5

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
    # posix=False preserves literal quotes in tokens instead of stripping 
    # them as shell syntax. This allows single quotes specified in the YAML 
    # to be passed directly to the tool.
    for tok in shlex.split(template, posix=True):
        for placeholder, value in subs.items():
            if placeholder in tok:
                tok = tok.replace(placeholder, value)
        rendered_tokens.append(tok)

    prefix = mf.resolve_invocation(manifest_data, tool_key)
    if tool.get("sudo"):
        # e.g. ConfigManBearPig needs to raw-socket UDP broadcast for
        # SCCM/MECM discovery - unprivileged sockets can't do that. Note:
        # sudo's password prompt (if any) talks to /dev/tty directly on
        # most systems, so it still works interactively even though stdout
        # is piped for the rolling panel.
        prefix = ["sudo"] + prefix
    return prefix + rendered_tokens


def run(args: RunArgs, manifest_data: dict) -> bool:
    print(live.colorize(f"\n{len(args.tools)} job(s) queued.\n", live.C.BOLD))

    all_ok = True
    for i, tool_key in enumerate(args.tools):
        try:
            argv = render_command(manifest_data, tool_key, args)
            live.stage(f"{tool_key} for {args.domain} / {args.username}")
            # We join the argv list directly without shlex.quote() for display.
            # Since execution uses a list (subprocess, no shell), shell quoting 
            # is irrelevant. shlex.quote() would visually double-wrap tokens 
            # that already contain literal quotes, confusing the user.
            live.info_line(live.colorize(' '.join(argv), live.C.DIM))
        except ConfigError as e:
            live.err_line(f"{tool_key}: {e}")
            all_ok = False
            continue

        rc, timed_out, output = live.run_streaming(
            argv,
            title=tool_key,
            num_lines=PANEL_LINES,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )

        if timed_out:
            live.err_line(f"{tool_key}: failed - exceeded {DEFAULT_TIMEOUT_SECONDS}s timeout")
            live.dump_tail(output)
            all_ok = False
        elif rc != 0:
            live.err_line(f"{tool_key}: failed (exit code {rc})")
            live.dump_tail(output)
            all_ok = False
        else:
            live.ok_line(f"{tool_key}: done")

        if i < len(args.tools) - 1 and DELAY_SECONDS > 0:
            live.wait_countdown(DELAY_SECONDS, label=f"Waiting {DELAY_SECONDS}s before next tool")

    width = 74
    print("\n" + live.colorize("=" * width, live.C.DIM))
    print(live.colorize("RUN SUMMARY", live.C.BOLD))
    print(live.colorize("=" * width, live.C.DIM))
    for tool_key in args.tools:
        print(f"  {tool_key:<18} {live.colorize('executed', live.C.DIM)}")
    print(live.colorize("=" * width, live.C.DIM))
    
    return all_ok
