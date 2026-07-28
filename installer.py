import os
import shutil
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass

import manifest as mf

@dataclass
class InstallResult:
    tool: str
    version: str = ""
    error: str = ""
    ok: bool = True

def check_prereqs():
    print("[*] Checking prerequisites...")
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
        print("\n[!] Missing prerequisites:")
        for m in missing:
            print(f"    - {m}")
        print("\nPlease install the missing prerequisites before continuing.")
        sys.exit(1)
        
    print("[+] All prerequisites are installed.\n")

def run_cmd(cmd, cwd=None):
    print(f"    $ {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stderr.strip())
    return res.returncode == 0

def install_tool(manifest, key):
    tool = manifest["tools"][key]
    kind = tool["kind"]
    tool_path = mf.tool_dir(manifest, key)
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    
    print(f"\n[*] {key} ({kind})")
    
    # 1. Clone if not exists
    if not tool_path.exists():
        repo = tool["repo"]
        if not run_cmd(["git", "clone", repo, str(tool_path)]):
            return False, f"git clone failed for {key}"
        print(f"    [+] Cloned {key} -> {tool_path}")

        branch = tool.get("branch")
        if branch:
            if not run_cmd(["git", "checkout", branch], cwd=tool_path):
                return False, f"git checkout {branch} failed for {key}"
            print(f"    [+] Checked out branch '{branch}'")
    else:
        print(f"    [*] {key} already cloned.")
        
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
        print(f"    $ {cmd_str}")
        res = subprocess.run(cmd_str, cwd=tool_path, shell=True, capture_output=True, text=True)
        if res.returncode != 0:
            print(res.stderr.strip())
            return False, f"install step failed for {key}: {cmd_str}"

    return True, ""

def install_all(manifest, only=None, force=False):
    check_prereqs()
    
    keys = [k for k in manifest["tools"] if (not only or k in only)]
    results = []
    
    for key in keys:
        r = InstallResult(tool=key)
        success, err = install_tool(manifest, key)
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
    print("\n" + "="*60)
    print("TOOL CHECK SUMMARY")
    print("="*60)
    all_ok = True
    for r in results:
        if r.ok:
            status = "[+] OK"
        else:
            status = f"[!] FAILED ({r.error})"
            all_ok = False
        print(f"  {r.tool:<25} {status}")
    return all_ok