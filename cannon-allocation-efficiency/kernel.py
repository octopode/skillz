"""Helpers for cluster allocation checks. See SKILL.md."""
import re

THREAD_HUNGRY = ("diamond", "mafft", "blastp", "blastx", "hmmsearch", "hmmscan",
                 "bowtie2", "bwa", "minimap2", "samtools", "mmseqs", "cd-hit",
                 "muscle", "iqtree", "raxml")
THREAD_FLAGS = ("--threads", "-p ", "-@", "--cpu", "--nthreads", "-nt", "--thread",
                "{threads}")


def split_rules(text):
    """Return {rule_name: body_text} for every rule/checkpoint in a Snakefile."""
    marks = [(m.group(1), m.start())
             for m in re.finditer(r"(?m)^(?:rule|checkpoint)\s+(\w+)\s*:", text)]
    out = {}
    for i, (name, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out[name] = text[start:end]
    return out


DYNAMIC_RES_PATTERN = (r"(?m)^\s*(threads|mem_mb|mem_mb_per_cpu|mem|runtime|slurm_partition|"
                r"gpu\w*|cpus_per_task|tasks)\s*[=:]\s*(lambda|[A-Za-z_][\w.]*\s*\()")


def check_allocations(snakefile_path, mem_ceiling_mb=32000, batch_hint="batch",
                      array_rules=()):
    """Static pre-submission checks. Returns [] when nothing is wrong.

    Each finding is {rule, issue, detail}. See SKILL.md '## Before you submit'.

    `array_rules`: the rules you will pass to --slurm-array-jobs (or ["all"]).
    For those it also flags resources that vary per job (lambda / function /
    `attempt`), because the SLURM executor submits every task of an array with
    the FIRST job's resources, and `group:` membership, because group jobs are
    never submitted as arrays. A profile's set-resources is not read here.
    """
    text = open(snakefile_path).read()
    findings = []
    arr = set(array_rules or ())
    for name, body in split_rules(text).items():
        code = "\n".join(l.split("#")[0] for l in body.splitlines())
        if "all" in arr or name in arr:
            dyn = re.findall(DYNAMIC_RES_PATTERN, code)
            if dyn or re.search(r"\battempt\b", code):
                what = ", ".join(sorted({d[0] for d in dyn})) or "attempt-scaled resources"
                findings.append({"rule": name, "issue": "array_resources_collapse",
                                 "detail": "%s vary per job, but every array task is "
                                           "submitted with the first job's values - "
                                           "tasks needing more are under-reserved" % what})
            if re.search(r"(?m)^\s*group\s*:", code):
                findings.append({"rule": name, "issue": "array_ignored_group",
                                 "detail": "group jobs are never submitted as arrays"})
        th = re.search(r"threads:\s*(\d+)", body)
        n = int(th.group(1)) if th else 1
        mem = re.search(r"mem_mb\s*=\s*(\d+)", body)
        if n > 1 and "{threads}" not in body:
            findings.append({"rule": name, "issue": "threads_not_passed",
                             "detail": "reserves %d cores but never interpolates "
                                       "{threads} into its command" % n})
        if batch_hint in name:
            if not mem:
                findings.append({"rule": name, "issue": "no_mem",
                                 "detail": "batch rule inherits the profile default"})
            if "runtime" not in body:
                findings.append({"rule": name, "issue": "no_runtime",
                                 "detail": "batch rule inherits the profile default"})
        if mem and int(mem.group(1)) > mem_ceiling_mb:
            findings.append({"rule": name, "issue": "memory_above_ceiling",
                             "detail": "%s MB requested, ceiling %s MB"
                                       % (mem.group(1), mem_ceiling_mb)})
        # Comments are not commands: a rule's trailing comment block (which runs up
        # to the next rule header) routinely names the tools used by LATER rules.
        low = "\n".join(l.split("#")[0] for l in body.lower().splitlines())
        for tool in THREAD_HUNGRY:
            if tool in low and not any(f in low for f in THREAD_FLAGS):
                findings.append({"rule": name, "issue": "tool_grabs_all_cores",
                                 "detail": "%s has no thread flag; it defaults to "
                                           "every visible core" % tool})
                break
    return findings


def pool_slice_size(n_units, workers, cap=20):
    """Largest slice that still gives every worker at least one slice.

    Floor, not ceil: 20 units over 8 workers must give 2 (10 slices), not 3 (7).
    `cap` bounds a slice for long runs (e.g. recycling a leaky worker process).
    """
    return max(1, min(cap, n_units // max(workers, 1)))


def audit_benchmarks(bench_dir, pattern="*.tsv"):
    """Per-rule reserved-vs-used table from snakemake `benchmark:` records.

    Expects bench_dir/<rule>/<id>.tsv. Returns a DataFrame with jobs, wall_h,
    cpu_h, cores_used (cpu_time/wall — compare against the rule's threads) and
    peak_rss_gb. See SKILL.md '## After it finishes' for reading the signatures.
    """
    import pandas as pd
    from pathlib import Path
    rows = []
    for rule_dir in sorted(p for p in Path(bench_dir).iterdir() if p.is_dir()):
        wall = cpu = rss = 0.0
        n = 0
        for f in rule_dir.glob(pattern):
            try:
                d = pd.read_csv(f, sep="\t").iloc[0]
            except Exception:
                continue
            wall += float(d.get("s", 0) or 0)
            cpu += float(d.get("cpu_time", 0) or 0)
            rss = max(rss, float(d.get("max_rss", 0) or 0))
            n += 1
        if n:
            rows.append({"rule": rule_dir.name, "jobs": n,
                         "wall_h": round(wall / 3600, 2),
                         "cpu_h": round(cpu / 3600, 2),
                         "cores_used": round(cpu / wall, 1) if wall else 0.0,
                         "peak_rss_gb": round(rss / 1024, 1)})
    return pd.DataFrame(rows).sort_values("cpu_h", ascending=False)

def assert_not_scheduled(dry_run_text, forbidden):
    """Offending lines in a Snakemake dry run that should not be scheduled.

    Use in a precondition gate before launching an expensive run: pass the dry-run
    output and the path patterns of work you believe is already finished (e.g.
    ["sites_dispersed/batch_", "matrices/batch_"]). Returns [] when clean, so it
    reads as an assertion.

    Why this exists: after renaming a finished stage's outputs, a mistake in the
    rename does not raise — Snakemake simply plans to recompute the stage. The
    only signal is the dry-run plan, and by the time the run is over it has
    already been paid for.

    NOTE: a dry run taken before a `checkpoint` rule has written its manifests
    cannot list the jobs behind that checkpoint. An empty return proves the
    forbidden work is not scheduled; it does NOT prove the plan is small.
    """
    hits = []
    for line in (dry_run_text or "").splitlines():
        for pat in forbidden:
            if pat in line:
                hits.append({"pattern": pat, "line": line.strip()})
                break
    return hits


def shipment_markers(paths_to_markers, root="."):
    """Verify a shipment landed by grepping installed files for marker strings.

    `paths_to_markers` maps a path (relative to `root`) to a string that must
    appear in it — take the marker from the change you just shipped. Returns a
    list of failures, empty when every marker is present.

    `tar -xzf` under `set +e` can silently extract nothing (a wrong $PWD after an
    early `cd` is the usual cause), and the job then runs happily against the old
    code. Exit codes do not catch this; content does.
    """
    import os
    missing = []
    for rel, marker in paths_to_markers.items():
        full = os.path.join(root, rel)
        try:
            with open(full, "r", errors="replace") as fh:
                if marker not in fh.read():
                    missing.append({"path": rel, "marker": marker,
                                    "reason": "marker absent"})
        except OSError as e:
            missing.append({"path": rel, "marker": marker, "reason": str(e)})
    return missing


# ---------------------------------------------------------------------------
# sacct accounting, including SLURM job arrays. See SKILL.md '## Job arrays'.
# ---------------------------------------------------------------------------

SACCT_ID_PATTERN = (r"^(?P<job>\d+)(?:_(?P<task>\d+|\[[^\]]*\]))?"
             r"(?:\+(?P<het>\d+))?(?:\.(?P<step>[\w-]+))?$")


def slurm_seconds(s):
    """'1-02:03:04', '02:03:04', '03:04', '01:19.773' -> seconds (float)."""
    s = (s or "").strip()
    if not s or s.upper() in ("INVALID", "UNLIMITED", "PARTITION_LIMIT"):
        return 0.0
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + sec


def slurm_mb(s, alloc_cpus=1):
    """'1840K', '2001M', '4G', '4000Mc' (per CPU), '4000Mn' -> MB (float)."""
    s = (s or "").strip()
    if not s:
        return 0.0
    m = re.match(r"^([\d.]+)([KMGTP]?)([cn]?)$", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    scale = {"": 1.0 / 1024 ** 2, "K": 1.0 / 1024, "M": 1.0, "G": 1024.0,
             "T": 1024.0 ** 2, "P": 1024.0 ** 3}[m.group(2)]
    mb = val * scale
    if m.group(3) == "c":
        mb *= max(int(alloc_cpus or 1), 1)
    return mb


def rule_from_comment(comment):
    """The snakemake SLURM executor writes 'rule_<name>[_wildcards_<w>]' (or
    'group_<name>') into --comment. For an ARRAY the wildcards are those of the
    first task only, so only the rule part is trustworthy."""
    m = re.match(r"^(?:rule|group)_(.+?)(?:_wildcards_.*)?$", comment or "")
    return m.group(1) if m else ""


def parse_sacct(text, sep="|"):
    """Fold `sacct -P` output (header line required) into one record per job
    or per array task.

    Handles regular IDs (123, 123.batch, 123.extern, 123.0), array tasks
    (123_4, 123_4.batch, 123_4.0), pending array placeholders (123_[5-10%2])
    and heterogeneous components (123+0). Ask sacct for at least
    JobID,AllocCPUS,Elapsed,TotalCPU,State; ReqMem, MaxRSS, JobIDRaw, ExitCode,
    Comment, JobName and Partition are used when present.

    Why this is not a one-liner:
      * MaxRSS lives only on the .batch/.N step rows -> max over steps.
      * TotalCPU on the allocation row is the sum of its steps in a full listing
        but reads 0 under `sacct -X` -> take max(allocation, sum of steps).
      * An array task's JobIDRaw is a DIFFERENT number from its JobID, and the
        last-dispatched task inherits the array's own id as its raw id. Key on
        JobID ('<array>_<task>'), never on JobIDRaw.
    Returns a list of dicts; see audit_sacct() for the derived columns.
    """
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return []
    header = lines[0].split(sep)
    if "JobID" not in header:
        raise ValueError("parse_sacct needs `sacct -P` output WITH its header line "
                         "(do not pass -n/--noheader)")
    units, order = {}, []
    for line in lines[1:]:
        row = dict(zip(header, line.split(sep)))
        m = re.match(SACCT_ID_PATTERN, row.get("JobID", "").strip())
        if not m:
            continue
        job, task, het, step = m.group("job", "task", "het", "step")
        key = job + ("_" + task if task else "") + ("+" + het if het else "")
        if key not in units:
            units[key] = {"unit": key, "job_id": job,
                          "array_task": int(task) if task and task.isdigit() else None,
                          "pending_tasks": task[1:-1] if task and task.startswith("[") else None,
                          "raw_id": "", "name": "", "partition": "", "alloc_cpus": 0,
                          "req_mem_mb": 0.0, "elapsed_s": 0.0, "_alloc_cpu_s": 0.0,
                          "_step_cpu_s": 0.0, "max_rss_mb": 0.0, "state": "",
                          "exit_code": "", "comment": "", "rule": ""}
            order.append(key)
        u = units[key]
        cpu = slurm_seconds(row.get("TotalCPU"))
        if step is None:  # the allocation row
            alloc = int(row.get("AllocCPUS") or 0)
            u.update(raw_id=row.get("JobIDRaw", ""), name=row.get("JobName", ""),
                     partition=row.get("Partition", ""), alloc_cpus=alloc,
                     req_mem_mb=slurm_mb(row.get("ReqMem"), alloc),
                     elapsed_s=slurm_seconds(row.get("Elapsed")),
                     state=(row.get("State") or "").split(" ")[0],
                     exit_code=row.get("ExitCode", ""), comment=row.get("Comment", ""),
                     rule=rule_from_comment(row.get("Comment", "")),
                     _alloc_cpu_s=cpu)
        else:
            u["_step_cpu_s"] += cpu
        u["max_rss_mb"] = max(u["max_rss_mb"], slurm_mb(row.get("MaxRSS")))
    out = []
    for k in order:
        u = units[k]
        u["total_cpu_s"] = max(u.pop("_alloc_cpu_s"), u.pop("_step_cpu_s"))
        out.append(u)
    return out


def audit_sacct(source, by="unit", serial_slack=0.10):
    """Reserved-vs-used table from sacct, at unit, array or rule level.

    `source` is `sacct -P` text (with header) or parse_sacct() records.
    by="unit"  : one row per job / array task.
    by="array" : one row per array (regular jobs are dropped) with the spread
                 statistics that matter for arrays: every task got the SAME
                 request, so look at elapsed tail (max/median) and memory
                 headroom (request / peak RSS) across tasks.
    by="rule"  : grouped on the rule parsed from Comment (snakemake executor).
    Pending placeholder rows (123_[5-10]) are reported in by="unit" only.
    Sorted by wasted core-hours (reserved - used), largest first.
    """
    import pandas as pd
    recs = parse_sacct(source) if isinstance(source, str) else list(source)
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["reserved_core_h"] = df["alloc_cpus"] * df["elapsed_s"] / 3600
    df["used_core_h"] = df["total_cpu_s"] / 3600
    df["waste_core_h"] = df["reserved_core_h"] - df["used_core_h"]
    df["util"] = (df["used_core_h"] / df["reserved_core_h"]).where(df["reserved_core_h"] > 0)
    # The abuse-monitor signature: a serial code in a parallel reservation shows
    # utilisation close to 1/AllocCPUS.
    df["serial_signature"] = (df["alloc_cpus"] > 1) & (
        df["util"] <= 1.0 / df["alloc_cpus"].clip(lower=1) + serial_slack)
    if by == "unit":
        return df.sort_values("waste_core_h", ascending=False).reset_index(drop=True)
    done = df[df["pending_tasks"].isna()]
    if by == "array":
        done = done[done["array_task"].notna()]
        key = "job_id"
    elif by == "rule":
        key = "rule"
    else:
        raise ValueError("by must be 'unit', 'array' or 'rule'")
    if done.empty:
        return done

    def _agg(g):
        med = g["elapsed_s"].median()
        peak = g["max_rss_mb"].max()
        res = g["reserved_core_h"].sum()
        used = g["used_core_h"].sum()
        return {
            "rule": ",".join(sorted(set(g["rule"]) - {""})),
            "units": len(g),
            "failed": int((~g["state"].isin(["COMPLETED", "RUNNING", "PENDING"])).sum()),
            "alloc_cpus": ",".join(str(x) for x in sorted(set(g["alloc_cpus"]))),
            "req_mem_mb": ",".join(str(round(x)) for x in sorted(set(g["req_mem_mb"]))),
            "peak_rss_mb": round(peak, 1),
            "median_rss_mb": round(g["max_rss_mb"].median(), 1),
            "mem_headroom": round(g["req_mem_mb"].max() / peak, 1) if peak else None,
            "elapsed_median_s": med,
            "elapsed_max_s": g["elapsed_s"].max(),
            "tail_ratio": round(g["elapsed_s"].max() / med, 2) if med else None,
            "reserved_core_h": round(res, 4),
            "used_core_h": round(used, 4),
            "waste_core_h": round(res - used, 4),
            "util": round(used / res, 3) if res else None,
            "serial_units": int(g["serial_signature"].sum()),
        }
    rows = []
    for name, g in done.groupby(key):
        r = _agg(g)
        r[key] = name
        rows.append(r)
    out = pd.DataFrame(rows).set_index(key)
    return out.sort_values("waste_core_h", ascending=False)


def array_chunks(n_units, max_array_size=10000, throttle=None, max_submit=None):
    """Split n_units into raw `sbatch --array` submissions that SLURM accepts.

    MaxArraySize bounds the task INDEX (valid indices 0..MaxArraySize-1), not
    just the count; on CANNON `--array=0-9999` is accepted and `1-10000` is
    rejected. Every task counts against MaxSubmitJobs, so pass the association
    or QOS limit as `max_submit` (CANNON user association: 10100; `test` QOS: 5).
    Returns [{offset, n, spec}]; in the job script use
    UNIT=$((OFFSET + SLURM_ARRAY_TASK_ID)). When n_units > max_submit the chunks
    cannot all be queued at once: submit the next only after the previous drains
    (a dependency does not help - a pending dependent still counts as submitted).
    """
    size = max_array_size if max_submit is None else min(max_array_size, max_submit)
    out, off = [], 0
    while off < n_units:
        n = min(size, n_units - off)
        spec = "0-%d" % (n - 1) + ("%%%d" % throttle if throttle else "")
        out.append({"offset": off, "n": n, "spec": spec})
        off += n
    return out


def array_mode_stalls(ready, jobs, array_limit=1000, max_array_size=10000):
    """Predict whether snakemake-executor-plugin-slurm 2.8.0 array mode will HOLD
    the selected jobs of one rule forever.

    The scheduler removes selected jobs from dag.ready_jobs before calling the
    executor; the plugin then compares what it was handed (`selected`) with the
    rule's jobs still ready (`remaining`) and returns without submitting when
    selected < remaining and selected < chunk (or selected == 1 and remaining >= 2).
    Snakemake already counts those jobs as running, so they are never offered
    again. `ready` = jobs of the rule ready at the same moment, `jobs` = free
    --jobs slots. This is a per-pass approximation: --jobs is shared with other
    rules and local jobs.
    """
    chunk = min(int(array_limit), int(max_array_size) - 1)
    selected = min(ready, jobs)
    remaining = ready - selected
    if selected <= 0:
        return False
    if selected == 1:
        return remaining >= 2
    return selected < remaining and selected < chunk
