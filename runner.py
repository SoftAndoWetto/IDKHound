#!/usr/bin/env python3
"""
runner.py

Rudimentary job runner. Takes inline flags (domain, user, pass, dc, tools)
from main.py, builds a flat job list, executes them sequentially with a
constant delay, and logs everything to SQLite + log files.
"""

import argparse
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import manifest as mf

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DB_FILE = STATE_DIR / "jobs.db"

# Constant wait between tools
DELAY_SECONDS = 5
# Constant timeout for all tools
DEFAULT_TIMEOUT_SECONDS = 600

# Substrings that indicate a credential/auth problem
AUTH_FAILURE_MARKERS = [
    "STATUS_LOGON_FAILURE",
    "STATUS_ACCOUNT_RESTRICTION",
    "STATUS_ACCOUNT_DISABLED",
    "STATUS_ACCOUNT_LOCKED_OUT",
    "STATUS_ACCOUNT_EXPIRED",
    "STATUS_PASSWORD_EXPIRED",
    "STATUS_PASSWORD_MUST_CHANGE",
    "KRB_AP_ERR_SKEW",
    "KDC_ERR_PREAUTH_FAILED",
    "KDC_ERR_C_PRINCIPAL_UNKNOWN",
    "invalid credentials",
    "authentication failed",
    "access is denied",
    "acceptsecuritycontext error",
    "login failed for user",
]

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

@dataclass
class Job:
    job_id: str
    target: str
    dc_ip: str
    account: str
    password: str
    tool: str
    job_type: str = "collect"
    status: str = "pending"
    attempts: int = 0
    error_type: str = ""
    error_message: str = ""
    started_at: str = ""
    finished_at: str = ""
    exit_code: Optional[int] = None
    output_path: str = ""
    log_path: str = ""
    tool_version: str = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    target        TEXT,
    dc_ip         TEXT,
    account       TEXT,
    password      TEXT,
    tool          TEXT,
    job_type      TEXT,
    status        TEXT,
    attempts      INTEGER,
    error_type    TEXT,
    error_message TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    exit_code     INTEGER,
    output_path   TEXT,
    log_path      TEXT,
    tool_version  TEXT
);
"""

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def connect_db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn

def save_job(conn: sqlite3.Connection, job: Job):
    conn.execute(
        """
        INSERT INTO jobs (job_id, target, dc_ip, account, password, tool, job_type,
                           status, attempts, error_type, error_message, started_at,
                           finished_at, exit_code, output_path, log_path, tool_version)
        VALUES (:job_id,:target,:dc_ip,:account,:password,:tool,:job_type,
                :status,:attempts,:error_type,:error_message,:started_at,
                :finished_at,:exit_code,:output_path,:log_path,:tool_version)
        ON CONFLICT(job_id) DO UPDATE SET
            status=excluded.status, attempts=excluded.attempts,
            error_type=excluded.error_type, error_message=excluded.error_message,
            started_at=excluded.started_at, finished_at=excluded.finished_at,
            exit_code=excluded.exit_code, output_path=excluded.output_path,
            log_path=excluded.log_path, tool_version=excluded.tool_version
        """,
        asdict(job),
    )
    conn.commit()

def load_jobs_dict(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("SELECT * FROM jobs")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return {r[0]: Job(**dict(zip(cols, r))) for r in rows}

def build_job_matrix(args: RunArgs) -> list:
    jobs = []
    for tool_key in args.tools:
        job_id = f"{args.domain}|{args.username}|{tool_key}"
        jobs.append(Job(
            job_id=job_id, target=args.domain, dc_ip=args.dc_ip,
            account=args.username, password=args.password, tool=tool_key
        ))
    return jobs

def sync_job_matrix(conn: sqlite3.Connection, args: RunArgs) -> list:
    existing = load_jobs_dict(conn)
    matrix = build_job_matrix(args)
    for job in matrix:
        if job.job_id not in existing:
            save_job(conn, job)
    return matrix

def render_command(manifest_data: dict, job: Job, args: RunArgs) -> list:
    tool = manifest_data["tools"][job.tool]
    template = tool.get("command_template")
    if not template:
        raise ConfigError(f"tool '{job.tool}' has no command_template in tools.yaml")

    output_dir = args.data_dir / job.target / job.account
    output_dir.mkdir(parents=True, exist_ok=True)

    subs = {
        "{domain}": job.target,
        "{username}": job.account,
        "{password}": job.password,
        "{dc_ip}": job.dc_ip,
        "{output_dir}": str(output_dir),
    }

    rendered_tokens = []
    for tok in shlex.split(template):
        for placeholder, value in subs.items():
            if placeholder in tok:
                tok = tok.replace(placeholder, value)
        rendered_tokens.append(tok)

    prefix = mf.resolve_invocation(manifest_data, job.tool)
    return prefix + rendered_tokens

def execute_job(manifest_data: dict, job: Job, args: RunArgs, conn: sqlite3.Connection) -> Job:
    job.attempts += 1
    job.status = "running"
    job.started_at = _now()
    save_job(conn, job)

    log_dir = args.log_dir / job.target / job.account
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{job.tool}.log"
    job.log_path = str(log_path)

    try:
        argv = render_command(manifest_data, job, args)
    except ConfigError as e:
        job.status = "failed"
        job.error_type = "config_error"
        job.error_message = str(e)
        job.finished_at = _now()
        save_job(conn, job)
        return job

    with open(log_path, "a") as logf:
        logf.write(f"\n--- attempt {job.attempts} at {job.started_at} ---\n")
        logf.write(f"$ {' '.join(shlex.quote(a) for a in argv)}\n\n")
        logf.flush()
        try:
            result = subprocess.run(argv, stdout=logf, stderr=subprocess.STDOUT,
                                     text=True, timeout=DEFAULT_TIMEOUT_SECONDS)
            job.exit_code = result.returncode
        except subprocess.TimeoutExpired:
            job.status = "failed"
            job.error_type = "timeout"
            job.error_message = f"exceeded {DEFAULT_TIMEOUT_SECONDS}s timeout"
            job.finished_at = _now()
            save_job(conn, job)
            return job
        except Exception as e:
            job.status = "failed"
            job.error_type = "tool_error"
            job.error_message = f"failed to launch: {e}"
            job.finished_at = _now()
            save_job(conn, job)
            return job

    job.finished_at = _now()
    output_dir = args.data_dir / job.target / job.account
    job.output_path = str(output_dir)

    if job.exit_code == 0:
        job.status = "done"
        job.error_type = ""
        job.error_message = ""
    else:
        job.status = "failed"
        try:
            log_text = log_path.read_text(errors="ignore").lower()
        except OSError:
            log_text = ""
        if any(marker.lower() in log_text for marker in AUTH_FAILURE_MARKERS):
            job.error_type = "auth_failure"
            job.error_message = f"exit {job.exit_code} - credential/auth failure"
        else:
            job.error_type = "tool_error"
            job.error_message = f"exit {job.exit_code} - see {log_path}"

    save_job(conn, job)
    return job

def run(args: RunArgs, manifest_data: dict, force: bool = False, dry_run: bool = False) -> bool:
    conn = connect_db()
    sync_job_matrix(conn, args)
    all_jobs = load_jobs_dict(conn)
    
    jobs_to_run = []
    for job in build_job_matrix(args):
        existing = all_jobs.get(job.job_id)
        if existing and existing.status == "done" and not force:
            continue
        jobs_to_run.append(existing if existing else job)

    if not jobs_to_run:
        print("[+] Nothing to run - all jobs already done (use --force to re-run).")
        print_summary(conn)
        conn.close()
        return True

    print(f"[*] {len(jobs_to_run)} job(s) queued.\n")

    if dry_run:
        for job in jobs_to_run:
            try:
                argv = render_command(manifest_data, job, args)
                print(f"    {job.tool}: {' '.join(shlex.quote(a) for a in argv)}")
            except ConfigError as e:
                print(f"    {job.tool}: [!] {e}")
        conn.close()
        return True

    all_ok = True
    for i, job in enumerate(jobs_to_run):
        print(f"[*] Running {job.tool} for {job.target} / {job.account}...")
        job = execute_job(manifest_data, job, args, conn)
        
        icon = "[+]" if job.status == "done" else "[!]"
        extra = f" ({job.error_type}: {job.error_message})" if job.status == "failed" else ""
        print(f"    {icon} {job.tool}: {job.status}{extra}")
        
        if job.status != "done":
            all_ok = False

        # Constant wait between tools
        if i < len(jobs_to_run) - 1 and DELAY_SECONDS > 0:
            print(f"[*] Waiting {DELAY_SECONDS}s before next tool...")
            time.sleep(DELAY_SECONDS)

    # TODO: Implement auto-upload via bloodhound-automation later
    print_summary(conn)
    conn.close()
    return all_ok

def print_summary(conn: sqlite3.Connection):
    jobs = list(load_jobs_dict(conn).values())
    icons = {"done": "[+]", "failed": "[!]", "pending": "[ ]", "running": "[~]"}

    print("\n" + "=" * 74)
    print("RUN SUMMARY")
    print("=" * 74)
    for j in sorted(jobs, key=lambda x: x.tool):
        icon = icons.get(j.status, "[?]")
        extra = f" - {j.error_type}: {j.error_message}" if j.status == "failed" else ""
        print(f"  {icon} {j.target:<20} {j.account:<15} {j.tool:<18} {j.status}{extra}")
    print("=" * 74)