#!/usr/bin/env python3
"""
bloodhound_upload.py

Uploads collector output (.json / .zip) to a configured BloodHound CE
instance over its REST API. Two entry points:

  - upload_after_run(...)   Called automatically right after `main.py run`
                             finishes, using whatever instance is currently
                             registered via bloodhound_manager.py. Quietly
                             no-ops if no instance is configured - this is
                             opt-in behavior layered on top of an existing
                             run, never a new hard requirement on it.

  - CLI (this file run directly, or `main.py upload`)
                             Manual upload of a specific file or a whole
                             folder, independent of any run. Useful for
                             re-uploading after a partial run, uploading
                             output that was moved/archived, or pointing at
                             an instance other than the one currently saved.

API flow (BloodHound CE /api/v2), confirmed against a known-working
reference client rather than assumed from memory:

    1. POST /api/v2/login                 -> {"data": {"session_token": ...}}
    2. POST /api/v2/file-upload/start      -> {"data": {"id": <job_id>}}
    3. POST /api/v2/file-upload/{job_id}   -> once per file, raw bytes as
                                               the body, Content-Type set
                                               per file (.json or .zip)
    4. POST /api/v2/file-upload/{job_id}/end -> closes the job and kicks
                                               off BloodHound's own ingest

Auth is JWT bearer, obtained via step 1 - fine here since this runs as a
short-lived CLI/hook, not a long-running integration (which is what the
HMAC API-key auth mode is for).

Credential handling matches bloodhound_manager.py's existing pattern:
  - manual instances store a password_env *reference*, never the raw value
    -> resolved from the environment at upload time.
  - automate instances store the actual password, since bloodhound-automate
    itself already pins a known (default or explicit) password in that
    file - there's nothing more sensitive to protect by re-hiding it.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

import bloodhound_manager as bhm

DEFAULT_TIMEOUT = 30
UPLOAD_TIMEOUT = 120  # collector output can be large; give it more room
ACCEPTED_SUFFIXES = {".json", ".zip"}


class UploadError(Exception):
    pass


@dataclass
class UploadResult:
    uploaded: list = field(default_factory=list)
    failed: list = field(default_factory=list)  # list of (path, error)
    ok: bool = True
    job_id: str = None
    error: str = ""


def _require_requests():
    if requests is None:
        raise UploadError(
            "the 'requests' package is required for BloodHound uploads - "
            "install it with: pip install requests"
        )


# --------------------------------------------------------------------------- #
# Credential resolution - reuses whatever bloodhound_manager.py already
# stored, so upload never asks for anything setup-manual/auto didn't
# already collect.
# --------------------------------------------------------------------------- #

def _resolve_password(inst: dict) -> str:
    if inst.get("mode") == "automate":
        pw = inst.get("password")
        if not pw:
            raise UploadError("stored automate instance has no password on record")
        return pw

    password_env = inst.get("password_env")
    if not password_env:
        raise UploadError("stored manual instance has no password_env configured")
    pw = os.environ.get(password_env)
    if not pw:
        raise UploadError(f"env var {password_env} is not set - export it before uploading")
    return pw


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #

def _auth_headers(token: str, content_type: str = None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if content_type:
        h["Content-Type"] = content_type
    return h


def login(base_url: str, username: str, password: str) -> str:
    _require_requests()
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/v2/login",
            json={"login_method": "secret", "secret": password, "username": username},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        raise UploadError(f"could not reach {base_url}: {e}")

    if resp.status_code != 200:
        raise UploadError(f"login failed ({resp.status_code}): {resp.text[:200]}")

    try:
        return resp.json()["data"]["session_token"]
    except (KeyError, ValueError, TypeError) as e:
        raise UploadError(f"unexpected login response: {e}")


def _start_job(base_url: str, token: str) -> str:
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/v2/file-upload/start",
        headers=_auth_headers(token),
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        raise UploadError(f"could not start upload job ({resp.status_code}): {resp.text[:200]}")
    try:
        return resp.json()["data"]["id"]
    except (KeyError, ValueError, TypeError) as e:
        raise UploadError(f"unexpected file-upload/start response: {e}")


def _end_job(base_url: str, token: str, job_id) -> None:
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/v2/file-upload/{job_id}/end",
        headers=_auth_headers(token),
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code not in (200, 204):
        raise UploadError(f"could not close upload job {job_id} ({resp.status_code}): {resp.text[:200]}")


def _content_type_for(path: Path) -> str:
    return "application/zip" if path.suffix.lower() == ".zip" else "application/json"


def _upload_one(base_url: str, token: str, job_id, path: Path) -> None:
    with open(path, "rb") as f:
        data = f.read()
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/v2/file-upload/{job_id}",
        headers=_auth_headers(token, _content_type_for(path)),
        data=data,
        timeout=UPLOAD_TIMEOUT,
    )
    if resp.status_code not in (200, 202):
        raise UploadError(f"upload failed ({resp.status_code}): {resp.text[:200]}")


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #

def collect_targets(path: Path) -> list:
    """
    Resolves a file or directory into a flat, sorted list of uploadable
    files (.json / .zip). A directory is walked recursively, so this works
    directly against a run's per-domain/per-user output_dir (one file per
    tool) as well as an arbitrary folder the user points at manually.
    """
    if path.is_file():
        if path.suffix.lower() not in ACCEPTED_SUFFIXES:
            raise UploadError(f"{path} is not a .json or .zip file")
        return [path]
    if not path.is_dir():
        raise UploadError(f"{path} does not exist")

    targets = sorted(
        p for p in path.rglob("*")
        if p.is_file() and p.suffix.lower() in ACCEPTED_SUFFIXES
    )
    if not targets:
        raise UploadError(f"no .json/.zip files found under {path}")
    return targets


# --------------------------------------------------------------------------- #
# Core upload routine - both the automatic hook and the manual CLI funnel
# through this, so there's exactly one place that talks to the API.
# --------------------------------------------------------------------------- #

def upload(base_url: str, username: str, password: str, targets: list,
           on_file_start=None, on_file_done=None) -> UploadResult:
    """
    Auth failure or a failed job-start aborts immediately (nothing was
    uploaded). A single file failing mid-batch is recorded and the rest
    are still attempted, then the job is still closed - so BloodHound
    processes whatever *did* make it in rather than a half-open job being
    left dangling because one file choked.
    """
    _require_requests()
    result = UploadResult()

    try:
        token = login(base_url, username, password)
        job_id = _start_job(base_url, token)
    except UploadError as e:
        result.ok = False
        result.error = str(e)
        return result

    result.job_id = job_id

    for path in targets:
        if on_file_start:
            on_file_start(path)
        try:
            _upload_one(base_url, token, job_id, path)
            result.uploaded.append(path)
            if on_file_done:
                on_file_done(path, True, None)
        except UploadError as e:
            result.failed.append((path, str(e)))
            result.ok = False
            if on_file_done:
                on_file_done(path, False, str(e))

    try:
        _end_job(base_url, token, job_id)
    except UploadError as e:
        result.ok = False
        result.error = (result.error + "; " if result.error else "") + f"failed to close job: {e}"

    return result


# --------------------------------------------------------------------------- #
# Automatic hook - called from main.py's cmd_run right after a collection
# job finishes.
# --------------------------------------------------------------------------- #

def upload_after_run(data_dir: Path, domain: str, username: str):
    """
    Best-effort auto-upload once a `run` completes. Returns None and prints
    nothing beyond a single skip line if no BloodHound instance is
    configured, credentials aren't resolvable, or there's simply nothing to
    upload - upload piggybacks on an existing run, it never turns into a
    reason the run itself is reported as failed.
    """
    import live_output as live

    inst = bhm.load_instance()
    if inst is None:
        return None

    try:
        password = _resolve_password(inst)
    except UploadError as e:
        live.warn_line(f"Skipping BloodHound auto-upload: {e}")
        return None

    output_dir = Path(data_dir) / domain / username
    try:
        targets = collect_targets(output_dir)
    except UploadError as e:
        live.warn_line(f"Skipping BloodHound auto-upload: {e}")
        return None

    live.stage(f"Uploading {len(targets)} file(s) to BloodHound ({inst['url']})")

    def _done(path, ok, err):
        if ok:
            live.ok_line(path.name)
        else:
            live.err_line(f"{path.name}: {err}")

    result = upload(inst["url"], inst["username"], password, targets, on_file_done=_done)

    if result.ok:
        live.ok_line(f"BloodHound upload complete - {len(result.uploaded)} file(s), job {result.job_id}.")
    else:
        summary = result.error or f"{len(result.failed)} file(s) failed"
        live.err_line(f"BloodHound upload finished with errors: {summary}")

    return result


# --------------------------------------------------------------------------- #
# Manual upload - shared by this file's own CLI and main.py's `upload`
# subcommand, so there's one implementation of "figure out credentials,
# then upload" regardless of which entrypoint it's called from.
# --------------------------------------------------------------------------- #

def run_manual_upload(file: Path = None, dir_: Path = None, url: str = None,
                       user: str = None, password_env: str = None,
                       password: str = None) -> bool:
    path = file or dir_
    if path is None:
        print("[!] Specify --file or --dir.")
        return False

    try:
        targets = collect_targets(Path(path))
    except UploadError as e:
        print(f"[!] {e}")
        return False

    inst = bhm.load_instance()

    base_url = url or (inst["url"] if inst else None)
    username = user or (inst["username"] if inst else None)

    resolved_password = password
    if not resolved_password and password_env:
        resolved_password = os.environ.get(password_env)
        if not resolved_password:
            print(f"[!] env var {password_env} is not set.")
            return False
    if not resolved_password and inst and not (url or user):
        # Only fall back to the saved instance's own credentials if the
        # caller isn't pointing at a *different* url/user - mixing a
        # manually-given target with the saved instance's password would
        # silently send it somewhere it wasn't meant for.
        try:
            resolved_password = _resolve_password(inst)
        except UploadError as e:
            print(f"[!] {e}")
            return False

    if not base_url or not username or not resolved_password:
        print("[!] No BloodHound instance configured and connection details are incomplete.")
        print("    Either run one of:")
        print("      python3 bloodhound_manager.py setup-manual --url ... --user ... --password-env ...")
        print("      python3 bloodhound_manager.py auto <Name>")
        print("    or pass --url/--user together with --password/--password-env explicitly.")
        return False

    print(f"[*] Uploading {len(targets)} file(s) to {base_url} as {username}:")
    for t in targets:
        print(f"      {t}")

    def _done(path, ok, err):
        status = "\u2713" if ok else "\u2717"
        line = f"    [{status}] {path.name}"
        if err:
            line += f" - {err}"
        print(line)

    result = upload(base_url, username, resolved_password, targets, on_file_done=_done)

    if result.ok:
        print(f"[+] Done. {len(result.uploaded)} file(s) uploaded, job {result.job_id} closed.")
    else:
        print(f"[!] Completed with errors: {result.error or ''}".rstrip())
        print(f"    {len(result.uploaded)} succeeded, {len(result.failed)} failed.")

    return result.ok


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Upload collector output (a file or folder) to a BloodHound CE instance"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="A single .json/.zip file to upload")
    src.add_argument("--dir", type=Path, dest="dir_", help="Folder to scan recursively for .json/.zip files")

    parser.add_argument("--url", default=None,
                         help="BloodHound URL (default: whatever setup-manual/auto registered)")
    parser.add_argument("--user", default=None,
                         help="Username (default: whatever setup-manual/auto registered)")
    parser.add_argument("--password-env", default=None,
                         help="Env var holding the password (preferred over --password)")
    parser.add_argument("--password", default=None,
                         help="Password directly - avoid where possible, prefer --password-env")

    args = parser.parse_args()
    ok = run_manual_upload(
        file=args.file, dir_=args.dir_, url=args.url, user=args.user,
        password_env=args.password_env, password=args.password,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()