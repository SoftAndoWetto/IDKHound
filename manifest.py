"""
manifest.py

Loads tools.yaml and resolves, for any tool entry, both its on-disk path and
the argv prefix needed to invoke it - regardless of whether that tool is a
Python venv script, a Python venv console-script, or a compiled Go/Rust
binary. This is the normalization layer: everything downstream (installer,
runner, update checker) treats a resolved invocation as an opaque argv list
and never needs to know which of the three it actually is.
"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
MANIFEST_FILE = ROOT / "tools.yaml"

VALID_KINDS = {
    "python-venv",
    "python-venv-installed",
    "python-venv-editable",
    "python-venv-requirements",
    "cargo-binary",
    "go-binary",
}


class ManifestError(Exception):
    pass


def load_manifest(path: Path = MANIFEST_FILE) -> dict:
    if not path.exists():
        raise ManifestError(f"Manifest not found at {path}")
    with open(path) as f:
        data = yaml.safe_load(f)
    if "tools" not in data:
        raise ManifestError(f"Manifest at {path} has no top-level 'tools' key")
    return data


def tools_root(manifest: dict) -> Path:
    return ROOT / manifest.get("tools_root", "tools")


def tool_dir(manifest: dict, key: str) -> Path:
    """The clone directory for a tool, e.g. tools/rusthound-ce."""
    tool = _get(manifest, key)
    return tools_root(manifest) / tool["path"]


def venv_python(manifest: dict, key: str) -> Path:
    """Path to a tool's venv python interpreter, for venv-kind tools."""
    tool = _get(manifest, key)
    venv_name = tool.get("venv", "venv")
    return tool_dir(manifest, key) / venv_name / "bin" / "python"


def venv_bin(manifest: dict, key: str, binary_name: str = None) -> Path:
    """Path to a console-script/binary inside a tool's venv/bin."""
    tool = _get(manifest, key)
    venv_name = tool.get("venv", "venv")
    name = binary_name or tool.get("binary")
    return tool_dir(manifest, key) / venv_name / "bin" / name


def resolve_invocation(manifest: dict, key: str) -> list:
    """
    Returns the argv prefix to invoke this tool, e.g.:
      python-venv            -> [.../venv/bin/python, .../entry.py]
      python-venv-installed  -> [.../venv/bin/gpoParser]
      python-venv-editable   -> [.../venv/bin/certihound]
      python-venv-requirements -> [.../venv/bin/python, .../entry.py]
      cargo-binary / go-binary -> [.../binary]

    Callers append their own args (e.g. --version, or actual collection
    flags) to whatever this returns.
    """
    tool = _get(manifest, key)
    kind = tool["kind"]

    if kind not in VALID_KINDS:
        raise ManifestError(f"Tool '{key}' has unknown kind '{kind}'")

    if kind in ("python-venv", "python-venv-requirements"):
        py = venv_python(manifest, key)
        entry = tool_dir(manifest, key) / tool["entry"]
        return [str(py), str(entry)]

    if kind in ("python-venv-installed", "python-venv-editable"):
        return [str(venv_bin(manifest, key))]

    if kind in ("cargo-binary", "go-binary"):
        return [str(tool_dir(manifest, key) / tool["binary"])]

    raise ManifestError(f"No invocation rule implemented for kind '{kind}'")


def is_installed(manifest: dict, key: str) -> bool:
    """
    Cheap on-disk check: does the resolved invocation's first element
    (the interpreter or binary) actually exist? Doesn't run anything -
    that's what verify_version in installer.py is for.
    """
    try:
        argv = resolve_invocation(manifest, key)
    except ManifestError:
        return False
    return Path(argv[0]).exists()


def _get(manifest: dict, key: str) -> dict:
    tools = manifest["tools"]
    if key not in tools:
        raise ManifestError(f"No tool named '{key}' in manifest")
    return tools[key]


def all_tool_keys(manifest: dict) -> list:
    return list(manifest["tools"].keys())