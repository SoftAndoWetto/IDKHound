import os
import shutil
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass

import manifest as mf
import live_output as live

@dataclass
class InstallResult:
    tool: str
    version: str = ""
    error: str = ""
    ok: bool = True

def check_prereqs():
    live.info_line("Checking prerequisites...")
    missing = []
    
    if not shutil.which("python3"):
        missing.append("python3")
    
    if not shutil.which("go"):
        missing.append("go (Install from https://go.dev/dl/)")
        
    if not shutil.which("cargo") or not shutil.which("rustc"):
        missing.append("rust (curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh)")
        
    if not shutil.which("krb5-config"):
        missing.append("libkrb5-dev (sudo apt install -y libkrb5-dev libgssapi-krb5-2 pkg-config)")
        
    if missing:
        live.err_line("Missing prerequisites:")
        for m in missing:
            print(f"      - {m}")
        print("\nPlease install the missing prerequisites before continuing.")
        sys.exit(1)
        
    live.ok_line("All prerequisites are installed.\n")

def run_cmd(cmd, cwd=None, title=None):
    """
    Runs a (usually short) command like `git clone`/`git checkout` through
    the rolling-panel streamer instead of capture_output=True, so a slow
    clone doesn't sit there looking dead either. On failure, dumps the full
    captured output (not just whatever the panel happened to be showing)
    so nothing is lost for debugging.
    """
    rc, timed_out, output = live.run_streaming(cmd, cwd=cwd, title=title or " ".join(cmd))
    if rc != 0:
        live.dump_tail(output)
    return rc == 0

def _current_branch(tool_path):
    """Cheap `git rev-parse --abbrev-ref HEAD` - no fetch, no mutation."""
    res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=tool_path, capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None

def ensure_branch(tool, tool_path, key="tool"):
    """
    If tools.yaml pins a branch for this tool (e.g. bloodhound-py's
    'bloodhound-ce', ConfigManBearPig's 'python'), make sure the clone is
    actually sitting on it - not just cloned once and left wherever HEAD
    happened to land. Just a rev-parse + optional checkout, no rebuild,
    so it's safe to run on every install/skip pass.
    """
    branch = tool.get("branch")
    if not branch:
        return True
    current = _current_branch(tool_path)
    if current == branch:
        live.ok_line(f"On correct branch '{branch}'")
        return True
    live.warn_line(f"On branch '{current or '?'}', expected '{branch}' - checking out...")
    ok = run_cmd(["git", "checkout", branch], cwd=tool_path, title=f"{key}: git checkout {branch}")
    if ok:
        live.ok_line(f"Switched to branch '{branch}'")
    return ok

def install_tool(manifest, key, force=False):
    tool = manifest["tools"][key]
    kind = tool["kind"]
    tool_path = mf.tool_dir(manifest, key)
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    
    live.stage(f"{key} ({kind})")

    already_cloned = tool_path.exists()

    # Fast path: tool is already cloned AND its resolved binary/interpreter
    # exists on disk (mf.is_installed is a cheap Path.exists() check, no
    # subprocess). Skip clone + build entirely instead of re-running every
    # install step - the branch is still verified since that's just a
    # rev-parse, not a rebuild.
    if not force and already_cloned and mf.is_installed(manifest, key):
        live.info_line("already installed - skipping clone/build (use --force to reinstall).")
        if not ensure_branch(tool, tool_path, key=key):
            return False, f"branch checkout failed for {key}"
        return True, ""

    # 1. Clone if not exists
    if not already_cloned:
        repo = tool["repo"]
        if not run_cmd(["git", "clone", repo, str(tool_path)], title=f"{key}: git clone {repo}"):
            return False, f"git clone failed for {key}"
        live.ok_line(f"Cloned {key} -> {tool_path}")

        branch = tool.get("branch")
        if branch:
            if not run_cmd(["git", "checkout", branch], cwd=tool_path, title=f"{key}: git checkout {branch}"):
                return False, f"git checkout {branch} failed for {key}"
            live.ok_line(f"Checked out branch '{branch}'")
    else:
        live.info_line("already cloned.")
        if not ensure_branch(tool, tool_path, key=key):
            return False, f"branch checkout failed for {key}"

    # 2. Build/Setup - driven entirely by the tool's 'install' steps in
    #    tools.yaml. Each step is a shell command run with cwd=tool_path,
    #    after placeholder substitution. This is what lets a tool like
    #    ConfigManBearPig install from a subfolder ("python/.") or a tool
    #    like bloodhound-py build out of a branch-specific layout, without
    #    any orchestrator code changes - only 'kind' still matters for how
    #    the tool gets *invoked* later (see manifest.resolve_invocation).
    install_cmds = tool.get("install")
    if not install_cmds:
        return False, f"No 'install' steps defined for {key} in tools.yaml"

    venv_path = tool_path / tool.get("venv", "venv")
    subs = {
        "{tool_dir}": str(tool_path),
        "{venv}": str(venv_path),
        "{python}": str(venv_path / "bin" / "python"),
        "{pip}": str(venv_path / "bin" / "pip"),
        "{binary}": tool.get("binary", ""),
    }

    for raw_cmd in install_cmds:
        cmd_str = raw_cmd
        for placeholder, value in subs.items():
            cmd_str = cmd_str.replace(placeholder, value)
        # shell=True + streamed through the rolling panel: a slow step like
        # rusthound-ce's `cargo build --release` now shows live compiler
        # output instead of sitting there silently looking hung.
        rc, timed_out, output = live.run_streaming(
            cmd_str, cwd=tool_path, shell=True, title=f"{key}: {cmd_str}"
        )
        if rc != 0:
            live.dump_tail(output)
            return False, f"install step failed for {key}: {cmd_str}"

    return True, ""

def install_all(manifest, only=None, force=False):
    check_prereqs()
    
    keys = [k for k in manifest["tools"] if (not only or k in only)]
    results = []
    
    for key in keys:
        r = InstallResult(tool=key)
        success, err = install_tool(manifest, key, force=force)
        if not success:
            r.ok = False
            r.error = err
        results.append(r)
        
    return results

def verify_version(manifest, key, result):
    tool = manifest["tools"][key]
    version_args = tool.get("version_cmd", [])
    
    # If version_args is [""], treat it as "call with no args"
    if version_args == [""]:
        version_args = []
        
    try:
        argv = mf.resolve_invocation(manifest, key) + version_args
        # Use a short timeout so it doesn't hang forever waiting for stdin
        res = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        
        # Most tools return 0 on --version, or return 1/2 with help text when called with no args.
        # As long as we get SOME output, the binary loaded and executed successfully.
        output = (res.stdout + res.stderr).strip()
        if output or res.returncode == 0:
            result.version = "Verified"
        else:
            result.error = f"Execution failed (Exit {res.returncode}, no output)"
            result.ok = False
    except subprocess.TimeoutExpired:
        result.error = "Timed out (likely waiting for input)"
        result.ok = False
    except Exception as e:
        result.error = f"Execution error: {e}"
        result.ok = False

def print_summary(results):
    width = 60
    print("\n" + live.colorize("=" * width, live.C.DIM))
    print(live.colorize("TOOL CHECK SUMMARY", live.C.BOLD))
    print(live.colorize("=" * width, live.C.DIM))
    all_ok = True
    for r in results:
        if r.ok:
            status = live.colorize("✓ OK", live.C.GREEN)
        else:
            status = f"{live.colorize('✗ FAILED', live.C.RED)} {live.colorize(f'({r.error})', live.C.DIM)}"
            all_ok = False
        print(f"  {r.tool:<25} {status}")
    print(live.colorize("=" * width, live.C.DIM))
    return all_ok