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


def check_allocations(snakefile_path, mem_ceiling_mb=32000, batch_hint="batch"):
    """Static pre-submission checks. Returns [] when nothing is wrong.

    Each finding is {rule, issue, detail}. See SKILL.md '## Before you submit'.
    """
    text = open(snakefile_path).read()
    findings = []
    for name, body in split_rules(text).items():
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

