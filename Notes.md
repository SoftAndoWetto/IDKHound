# AD Collector Orchestrator — Planning Notes

## Goal
Build a single orchestration layer that installs, version-checks, runs, and
uploads output from a set of heterogeneous BloodHound-adjacent AD collection
tools (Python, Go, Rust) across multiple targets/accounts, then uploads
results via `bloodhound-automation`.

---

## Tools in Scope

| Tool | Language | Repo | Binary/Entry Location |
|---|---|---|---|
| BloodHound.py | Python (venv) | dirkjanm/BloodHound.py | `BloodHound.py/bloodhound.py` |
| RustHound-CE | Rust (cargo) | g0h4n/RustHound-CE | `RustHound-CE/target/release/rusthound-ce` |
| MSSQLHound | Go | SpecterOps/MSSQLHound | `MSSQLHound/mssqlhound` |
| SCOMHound | Python (venv) | SpecterOps/SCOMHound | `SCOMHound/scomhound.py` |
| GPOHound | Python (venv) | cogiceo/GPOHound | `GPOHound/gpohound.py` |
| gpoParser | Python (venv, installed) | synacktiv/gpoParser | `gpoParser/venv/bin/gpoParser` |
| Certihound | Python (venv, editable) | 0x0Trace/certihound | `certihound/venv/bin/certihound` |
| ShareHound | Go | jazofra/sharehound | `sharehound/Go/sharehound.exe` (check Linux build target — see notes) |

### Notes on install/build quirks
- Not all tools use the same isolation method (pipx/uv/manual venv) — **this is
  fine**, the manifest just needs a `kind` field per tool so the orchestrator
  normalizes at the *invocation* layer, not the install layer.
- **ShareHound build command** in the README uses Windows-style paths/output
  (`sharehound.exe`, backslash path). If running the orchestrator on Linux,
  verify/rebuild with `go build -o sharehound ./cmd/sharehound` instead of
  blindly trusting the Windows example.
- gpoParser and Certihound install via `pip install .` / `pip install -e`,
  which drops a console-script binary inside their own `venv/bin/` — different
  shape than BloodHound.py/SCOMHound/GPOHound, which are invoked as
  `venv/bin/python <entry>.py`.
- **gpoParser has an "enrich" feature** — needs closer review to confirm
  whether its output is BHCE-ingestible or a separate report format. Don't
  assume schema compatibility without checking.
- GPOHound/gpoParser (GPO analysis) and ShareHound (share/ACL enum) may
  produce **supplementary reports**, not BHCE-ingest JSON — don't assume every
  tool's output goes through the same upload pipe.

---

## Core Design Decision: Normalize at the Invocation Layer

Don't try to force every tool through one install method (pipx/uv only). Instead,
define a **YAML manifest** per tool describing:
- how it's installed/built
- how to invoke it (`resolve_invocation()` returns an argv list regardless of
  whether that's `[venv/bin/python, entry.py]`, `[venv/bin/binary]`, or a
  plain compiled binary path)
- how to check version
- how to update

```yaml
tools:
  bloodhound-py:
    kind: python-venv
    path: BloodHound.py
    venv: venv
    entry: bloodhound.py
    repo: https://github.com/dirkjanm/BloodHound.py
    version_cmd: ["--version"]
    update: [git pull, "pip install ."]

  rusthound-ce:
    kind: cargo-binary
    path: RustHound-CE
    binary: target/release/rusthound-ce
    repo: https://github.com/g0h4n/RustHound-CE
    version_cmd: ["--version"]
    update: [git pull, "cargo build --release"]

  mssqlhound:
    kind: go-binary
    path: MSSQLHound
    binary: mssqlhound
    repo: "https://github.com/SpecterOps/MSSQLHound"
    version_cmd: ["--version"]
    update: [git pull, "go build -o mssqlhound ./cmd/mssqlhound"]

  scomhound:
    kind: python-venv
    path: SCOMHound
    venv: venv
    entry: scomhound.py
    repo: "https://github.com/SpecterOps/SCOMHound"
    version_cmd: [""]  # No version tag
    update: [""]

  gpohound:
    kind: python-venv
    path: GPOHound
    venv: venv
    entry: gpohound.py
    repo: "https://github.com/cogiceo/GPOHound"
    version_cmd: [""]  # No version tag
    update: [""]

  gpoparser:
    kind: python-venv-installed
    path: gpoParser
    venv: venv
    binary: venv/bin/gpoParser
    repo: "https://github.com/synacktiv/gpoParser"
    version_cmd: [""]  # No version tag
    update: [""]

  certihound:
    kind: python-venv-editable
    path: certihound
    venv: venv
    binary: venv/bin/certihound
    repo: "https://github.com/0x0Trace/Certihound"
    version_cmd: ["--version"]
    update: [""]

  sharehound:
    kind: go-binary
    path: sharehound/Go
    binary: sharehound.exe
    repo: "https://github.com/p0dalirius/ShareHound"
    version_cmd: ["--version"]
    update: [""]
```

Adding a new tool later = new YAML entry, not new orchestrator code.

`resolve_invocation(tool_manifest) -> list[str]` switches on `kind` and returns
the command prefix. Everything downstream (running, version-checking, logging)
treats the result as an opaque argv list — it doesn't care how the tool is
isolated.

---

## Update Checking

- `git fetch` then compare local HEAD vs `origin/HEAD` — only pull + rebuild if
  there's an actual diff. Don't rebuild Rust/Go on every run for nothing.
- Pin known-good commits (a lockfile) rather than auto-tracking HEAD — you want
  to *choose* when to bump versions, especially mid-engagement, so a broken
  upstream commit doesn't silently break the pipeline.
- Log tool version + commit hash alongside every collection run's output, so
  you always know exactly what produced what data later.

---

## Job Matrix (targets × accounts × tools)

Flatten the Cartesian product of targets, accounts, and tools into a **flat
list of discrete job records** up front, rather than nested loops buried in
run logic.

```python
jobs = []
for target in config.targets:
    for account in target.accounts:
        for tool in target.tools_to_run:   # not all tools apply to all targets
            jobs.append(Job(target=target, account=account, tool=tool, status="pending"))
```

Why flatten first instead of just running inline:
- Persist the full job list to state **before** anything runs — a crash
  mid-run leaves a complete picture of what was supposed to happen, not just
  what happened before it died.
- Can filter/reorder freely (e.g. run all `bloodhound-py` jobs across every
  account before moving to `rusthound-ce`; interleave per-account with delays
  for lockout spacing) without touching execution code.
- Clean resume: `jobs = [j for j in load_state() if j.status != "done"]`.
- Selective parallelism: different tools against the same account can run
  concurrently (same discrete auth, no compounded lockout risk), but different
  accounts against the same target should be serialized with a delay.

---

## State Tracking

Use **SQLite** over raw JSON once doing bulk multi-account runs — need to
query "show me every failed job" or "everything still pending for this
target" without hand-parsing a giant array, and it survives concurrent writes
better if parallelized.

Per-job record:

```json
{
  "job_id": "corp.local|svc_collector|bloodhound-py",
  "target": "corp.local",
  "account": "svc_collector",
  "tool": "bloodhound-py",
  "status": "done | failed | running | pending",
  "attempts": 1,
  "started_at": "...",
  "finished_at": "...",
  "exit_code": 0,
  "output_path": "out/corp.local/svc_collector/bloodhound-py.zip",
  "log_path": "logs/corp.local/svc_collector/bloodhound-py.log",
  "tool_version": "commit abc123 / v4.3.1"
}
```

Track the **upload step** (`bloodhound-automation`) as its own job in the same
state tracker — a failed upload is distinct from a failed collection, and you
don't want to silently lose data if collection succeeded but upload failed.

---

## Lockout / Auth Safety

- Don't blast fully parallel auth attempts by default — concurrent
  LDAP/SMB auth floods can trip account lockout policies or EDR.
- Rate-limit/delay between auth attempts per account, configurable per
  engagement.
- Serialize different accounts against the same target with spacing; tools
  run against the *same* account/target combo can be more safely parallelized
  since it's one discrete auth event.

---

## Uploading — via `bloodhound-automation`

Decision: use `bloodhound-automation` rather than hand-rolling BHCE ingest API
calls or direct Neo4j writes — simpler, self-contained.

```
python3 bloodhound-automation.py data -z test.zip my_project
```

- Orchestrator's config doesn't need BHCE API tokens — just whatever
  `bloodhound-automation` itself expects (likely its own Neo4j-facing
  config/creds).
- Treat the upload call as its own subprocess job in the state tracker, same
  as any collector invocation — same timeout/error-handling treatment.
- **To verify before bulk automation:** confirm `bloodhound-automation`
  targets the correct ingest schema for your BHCE version — some automation
  wrappers assume Community Edition, others assume legacy/pre-CE
  Neo4j-direct BloodHound, and ingest formats shifted between them. Do one
  manual test upload first.

---

## Error Handling

- **Distinguish failure types**, don't lump into one "failed" bucket:
  - Auth failure (bad creds) → account likely unusable, don't retry.
  - Network/timeout (transient) → worth one retry.
  - Tool crash/bug → log full stderr, don't retry, flag for manual review.
- **Timeouts on every subprocess call** (`subprocess.run(..., timeout=N)`) —
  some collectors can hang on a bad DC connection rather than erroring
  cleanly. LDAP-heavy tools need more headroom than quick SMB share enum.
- **Isolate job failures** — wrap each job execution in its own try/except,
  log, mark failed in state, move to the next job. One job's crash should
  never kill the whole batch.
- Capture stdout/stderr per run to a per-tool-per-target log file, not just
  terminal output.
- Timestamp everything, for correlating with target-side logs later.

---

## Output Handling

Two output classes, don't assume one pipe fits all:

1. **BHCE-ingestible** (zip/JSON matching the BHCE shared collection schema)
   → goes into the upload queue (`bloodhound-automation`).
2. **Supplementary reports** (GPO analysis, share enumeration, etc.) → stored
   /archived, cross-referenced by hostname/domain, but not blindly pushed
   through the BloodHound upload path.

Confirmed: most tools in scope already emit BHCE-compatible output, but
**gpoParser's "enrich" feature needs closer manual review** before assuming
it fits either bucket cleanly.

---

## Open Items / Things to Verify Before Building
- [ ] Confirm actual Linux build output for ShareHound (`.exe` naming looks
      copy-pasted from Windows instructions).
- [ ] Manually run gpoParser's "enrich" feature and inspect output schema.
- [ ] Manually run each tool once and inspect JSON output before wiring bulk
      automation — several of these (SCOMHound, MSSQLHound, Certihound,
      GPOHound) are newer/actively developed, don't assume schema stability
      across versions.
- [ ] Confirm `bloodhound-automation` matches your BHCE version's ingest
      schema (CE vs legacy) with one manual test upload.
- [ ] Decide default concurrency/delay settings for lockout safety before
      running against a real environment.

---

## Layout
Something along these lines

```
hound-orchestrator/
├── orchestrator/              # your actual Python code
│   ├── manifest.py
│   ├── jobs.py
│   ├── runner.py
│   ├── state.py
│   └── update.py
├── tools.yaml                 # the manifest
├── config.yaml                # targets/accounts/creds-refs
├── tools/                     # ALL third-party repos live here, nothing else
│   ├── bloodhound-py/         # cloned repo, venv inside
│   ├── rusthound-ce/
│   ├── mssqlhound/
│   ├── scomhound/
│   ├── gpohound/
│   ├── gpoparser/
│   ├── certihound/
│   ├── sharehound/
│   └── bloodhound-automation/
├── data/
│   ├── <target>/
│   │   └── <account>/
│   │      ├── <tool>.zip (or .json/.html for reports)
│   │      └── <tool>.log
├── state/
│   └── jobs.db                # sqlite
└── .env                       # secrets referenced by config.yaml, gitignored
```

---

## Next Step (proposed, not yet built)
Draft the orchestrator skeleton:
- Manifest loader (parses the tools YAML, exposes `resolve_invocation()`)
- Job matrix builder (targets × accounts × tools → flat job list)
- SQLite-backed state store (job status, retries, timestamps, output/log paths)
- Subprocess runner (per-job timeout, stdout/stderr capture, failure-type
  classification)
- Update checker (git fetch/compare, rebuild only on diff)
- Upload step wired in as its own job type calling `bloodhound-automation`
- Make sure that you cant run any of the tools without first having a bloodhound instance (either configured manually with the details or using bloodhound automate which also stores the details in the same location but is set up automatically)
- Adding a feature to absolutely nuke bloodhound-automate completely (containers, volumes, everything) incase anything happens with it as it has with me
- Bloodhound-automate will be created with this command "python3 bloodhound-automation.py start -bp 10001 -np 10501 -wp 8001 <Name>" leave the numbers as default but give the optional flag to change them, also making sure these get stored once the container is up and working (bh-automate will list the credentials when it is)

- When checking install if its a branch (Like ConfigManBearPig or BloodHound.py) add a check to make sure theyre in the right brach just in case
- Also add -u, -p and -d as instead of only --username, --password and --domain