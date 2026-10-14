---
name: cannon-allocation-efficiency
description: "Verify that cluster jobs can actually saturate the CPU cores and memory they reserve — before submitting and after running. Use when dispatching Snakemake or SLURM work to a shared HPC cluster, when a rule reserves multiple cores or tens of GB, when a stage runs far slower than its compute would suggest, or when auditing fairshare/core-hour usage. Covers: passing {threads} through to the command, worker pools that cannot fill their slices, libraries that grab every visible core inside an already-parallel loop, batch granularity against heavy-tailed per-unit cost, and reserved-vs-used auditing from sacct and snakemake benchmark records."
metadata:
  authored_via: "claude-science"
  published_by: "agent"
  published_at: "2026-10-13T14:07:18.747Z"
  authored_by: "agent"
  authored_at: "2026-10-13T14:07:04.548Z"
  authoring_session: "bf573051-5ad9-4c15-9728-cfc8fe6f8ac6"
  last_modified_by: "agent"
  last_modified_at: "2026-10-13T14:07:04.553Z"
---

# Cluster allocation efficiency

**A reserved core is charged to the group whether or not it does work.** On a
fairshare scheduler, a job that reserves 8 cores and uses 1 costs the lab seven
cores of future priority for nothing. Underuse is not a missed optimisation; it
is spending someone else's allocation.

This skill is the check to run *before* submitting and the audit to run *after*.

## Before you submit

Run the static checks on the workflow:

```python
from pprint import pprint
pprint(check_allocations("Snakefile"))        # [] means nothing to fix
```

It flags four failure modes, all of which have shipped to a real cluster:

1. **Reserves cores, never passes them.** `threads: 8` with a shell command that
   never interpolates `{threads}`. The rule gets 8 cores and runs single-threaded.
2. **Batch rule with no `mem_mb` or `runtime`.** It inherits profile defaults
   that were sized for something else — usually far too much.
3. **Absurd memory.** A request well above measured peak delays scheduling and
   stops other jobs packing onto the node, even though memory is not itself billed.
4. **A thread-hungry tool with no thread flag.** DIAMOND, MAFFT, BLAST, bowtie2,
   samtools and friends default to *every visible core*. Inside a worker loop that
   is already parallel, N workers x N cores oversubscribes the allocation and
   everything slows down.

Then check anything that splits work across a pool:

```python
pool_slice_size(n_units=20, workers=8)   # -> 2, giving 10 slices for 8 workers
```

**A pool must produce at least as many slices as it has workers.** A chunk size
tuned when the pool received the whole job is wrong once the work is batched for
a scheduler: 20 units at `slice_size=20` is ONE slice, so one worker runs and the
rest idle. Use floor division, not ceil — ceil gives 3 units/slice for 20 units
over 8 workers, hence 7 slices, and one core still sits idle.

Passing `{threads}` is necessary and **not sufficient**. The question to ask of
every rule is not "did we pass threads" but **"can this code fill what we
reserved"**.

## While it runs

Measure the rate from the workflow's own `benchmark:` records rather than
extrapolating:

```python
audit_benchmarks("logs/bench")     # per-rule wall, CPU-h, cores used, peak RSS
```

**Do not estimate a completion rate from a short window.** A 4-minute sample that
catches one completion implies whatever Poisson noise hands you — a real case
produced "43 h remaining" against a true figure near 12 h. Use a window long
enough to contain tens of events, and say which window the number came from.

Expect the tail to be slower than the mean whenever per-unit cost is heavy-tailed
and the planner isolates expensive units into their own batches.

## After it finishes

```python
rows = audit_benchmarks("logs/bench")
```

Read `cores_used` (CPU-time / wall) against the rule's declared `threads`:

| signature | diagnosis |
|---|---|
| `cores_used` ~ 1, threads N | single-threaded in a multi-core reservation — check `{threads}` reaches the command |
| `cores_used` ~ N/k, stable | the pool fills only k of N workers — check slice/chunk sizing |
| `cores_used` high early, low late | heavy-tailed unit cost; the batch ends when its slowest unit does |
| wall >> CPU across all jobs | I/O bound or waiting on a shared filesystem, not CPU starved |

For the heavy-tailed case the fix is **granularity, not more cores**: one unit per
job instead of twenty removes the intra-batch idle entirely, at the cost of more
scheduler entries. Per-job overhead of seconds against per-unit cost of minutes
is a trade worth making.

## SLURM and Snakemake specifics

* **Successful job logs are deleted by default**, which makes per-rule attribution
  from `sacct` impossible after the fact. Set `slurm-keep-successful-logs: true`
  and `slurm-efficiency-report: true` in the profile; the executor plugin will do
  the accounting itself. Add `slurm-delete-logfiles-older-than: 30`.
* **Snakemake auto-loads `profiles/default` as the workflow profile**, and its
  `cores:` setting silently clamps every rule's threads. If declared threads are
  being ignored, this is why — pin `--workflow-profile none` and set `cores`
  explicitly in the profile you meant to use. **Put that flag in the launcher
  script, and then actually use the launcher.** A project whose
  `snake_cannon.sh` already passed `--workflow-profile none` still got every rule
  clamped to 4, because an ad-hoc driver called `snakemake --profile …` directly
  and skipped it. The symptom is indistinguishable from a misconfigured profile:
  check which command the driver ran before blaming the YAML.
* **`set-resources:` in a profile OVERRIDES the Snakefile**, so a resource
  declared in both places is governed by the profile. Halving a rule's threads
  and doubling its `runtime:` in the Snakefile accomplishes nothing if the
  profile pins that rule's runtime — the batch keeps the old cap, runs longer on
  fewer cores, and dies at the time limit. Grep the profile for the rule name
  before changing a resource in the Snakefile.
* **A concurrency cap below the number of planned batches** costs wall time for no
  saving in core-hours. If the DAG plans 83 batches, a cap of 40 runs half of them
  behind the other half; the work and the charge are identical either way.
* **Never write "wait for the queue to drain" into a driver that is itself queued.**
  It will wait on its own job id forever.
* Pin BLAS in any rule that forks a worker pool: `OMP_NUM_THREADS=1
  OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1`. Each worker otherwise spawns its own
  BLAS threads on top of the pool.


## The driver is a job too

A workflow driver — the long-lived snakemake process that submits children and
waits — is itself a scheduled job, and it is the easiest allocation in the whole
run to get wrong. It does no compute: it polls `squeue`, parses, and sleeps.

Measured on a real sweep: 13 driver jobs reserved **91.3 core-h and used 2.7**
(2.9%), with a peak RSS of 1.7 GB against 198.7 GB reserved. The two single
worst jobs in the entire window — by wasted core-hours — were drivers, not
compute.

Give a driver **1 core and a couple of GB**, and size its `--time` generously
instead; wall time is what it actually needs. A driver on 2 cores is paying
double for a process that cannot use one.

Note the ceiling on this fix, and do not overstate it in a report: dropping 2
cores to 1 recovers *half the reservation*, not the idle fraction. A 24 h driver
reserving 48 core-h and using 1.6 recovers 24, not 46.4. The rest is only
recoverable by not making the driver a scheduled job at all.

## Audit what you actually used

Run this after any multi-day campaign; it needs nothing but `sacct`.

```python
# group by whether the job is a driver or a worker, then compare
# reserved = AllocCPUS x Elapsed   against   used = TotalCPU
out = subprocess.run(["sacct", "-u", user, "-S", since, "-P", "-n", "--format",
                      "JobID,JobName,Partition,AllocCPUS,ReqMem,Elapsed,TotalCPU,MaxRSS,State"],
                     capture_output=True, text=True).stdout
```

`MaxRSS` appears only on the `.batch`/`.extern` sub-steps, so fold those into
their parent `JobID` before reading peak memory — a naive parse reports 0.

Rank jobs by `reserved - used` rather than by efficiency percentage. A 2-core
job at 3% efficiency wastes less than a 48-core job at 60%, and only the
absolute figure tells you where to spend the effort.

Two signatures to expect, with opposite fixes:

| what you see | what it means |
|---|---|
| `cores_used` far below the allocation, stable across jobs | the pool cannot fill the reservation — size down, or re-granulate |
| `cores_used` **above** the allocation | library threads stacking on pool workers — pin BLAS to 1 |

The second is worth treating as urgent on a shared partition. A job measured at
115% of its 4-core reservation is taking cores from whoever else landed on that
node, and it will not appear in any efficiency report as a problem — those only
flag *under*-use.

## Utilisation, not average cores: the measurement that catches a serial job

There are two ways to summarise how much CPU a job used, they disagree, and only one of
them matches what the cluster's abuse monitor computes.

**Average cores over wall time** — `TotalCPU / Elapsed` — answers "how many cores was this
job using on average". A job that briefly forks 8 workers and then runs serially for an
hour scores well above 1.

**Utilisation** — `TotalCPU / (AllocCPUS x Elapsed)` — answers "what fraction of the
reservation was used". This is what FASRC's Job Defense Shield and similar monitors
report, and what they look for is `CPU-Util ~ 100% / AllocCPUS`: the signature of a serial
code holding a parallel reservation — 4 cores at 25%, 2 cores at 50%.

Right-sizing against the first number while the monitor reads the second is how you reduce
an allocation, feel finished, and get the email anyway. Size to measured cores *and* check
the resulting utilisation clears ~90%; if it cannot, the code is serial and the honest
allocation is 1.

```bash
sacct -j <ids> -P --format=JobID,AllocCPUS,TotalCPU,Elapsed,State
```

Two traps in reading it. `TotalCPU` is **zero at the allocation level** — it lives on the
`.batch` / `.0` sub-steps, so aggregate per job id and take the max across steps, exactly
as with `MaxRSS`. And `TotalCPU` is **not populated while a job runs**; for a live job use
`sstat -j <jobid>.0 --format=AveCPU,NTasks,AveRSS` instead.

### Attributing a job to a rule when the job name is a UUID

Snakemake's SLURM executor names every job after the *workflow* uuid, so `sacct` alone
cannot tell you which rule wasted the time — every row carries the same name. The rule is
in the log path the executor writes:

```
.snakemake/slurm_logs/rule_<rulename>/<wildcards>/<jobid>.log
```

Walk that tree to build `rule -> [jobid]`, then aggregate `sacct` per rule. Rank rules by
`reserved - used` rather than by utilisation: a rule at 45% over 600 wall-hours wastes far
more than one at 17% over 7.

### GPU rules are the usual offenders

A rule that holds a GPU often needs almost no CPU, and inherits a CPU count from whatever
the neighbouring CPU rules use. Two folding rules measured **0.68 and 0.72 cores against 4
allocated** — 17%, precisely the flagged pattern — because their alignments were built in
an earlier stage, leaving the job to feed a GPU and little else. Check GPU rules
separately; their CPU request is rarely thought about.

### Dropping an unused reservation costs nothing

This is the objection that stops people, so state it plainly: if a rule measured 2.0 cores
used out of 8, cutting it to 2 does not slow it down and no runtime cap needs raising. You
are giving back cores that were idle. Only raise `runtime` when you cut *below* measured
use — and a scheduler profile's `set-resources:` overrides the workflow file, so the cap
has to be raised in both places or it silently stays.

### Finishing a running job can be the frugal choice

When a fix lands mid-run, compare the waste of letting the job finish against the cost of
redoing the work already done. Six jobs 3.5 hours into a 7-hour stage at 8 cores using 3
waste ~105 core-hours if left alone and ~250 if killed and restarted at 4, because the 3.5
hours already spent are discarded. Apply the fix to the next submission and let the current
one run. Good cluster citizenship is about total waste, not about killing things quickly.

## Stage expensive runs behind a cheap, visible gate

The waste this prevents is not idle cores — it is **recomputing work you already
have**, or launching a long run on a shipment that never arrived. Both are silent:
the workflow does not error, it just starts jobs.

The trap is structural, not technical. You write a precondition check (migrate
renamed outputs, verify the code shipped, dry-run and confirm the finished stage is
not rescheduled), and then you put it at the top of the same submitted job as the
expensive run. It works — and its verdict is invisible until the expensive part
finishes, which is precisely when knowing it no longer helps. A go/no-go you read
four hours late is not a gate.

**Split it into two submissions.**

```
job 1 (partition=test, ~2 min, 1 approval)
    ship code  ->  verify markers  ->  migrate paths  ->  dry-run  ->  assert
    return the verdict as the job's output
                      |
             read it, THEN decide
                      |
job 2 (the real allocation)   only if job 1 passed
```

Two approvals instead of one, in exchange for the decision being actionable when
it is made. Measured on a real workflow (3,249 matrices, 1,652 trees, 111 flags,
64 batch parts over Lustre), the whole gate job — ship, migrate, unlock, dry-run,
assert — ran in **2 min 16 s**. A dry run is cheap; only its placement was not.

### What the gate should assert

* **The shipment arrived.** `tar -xzf` under `set +e` can silently do nothing.
  Grep the *installed* file for a marker string from the change you just made —
  never trust the exit code. Classic cause: `cd $DUR` before
  `tar -xzf $PWD/archive.tar.gz`, which makes `$PWD` the project tree rather than
  the job directory, so the archive is not where you say it is.
* **Finished work is not rescheduled.** Dry-run and grep the plan for the
  completed stage's batch pattern. If it appears, a rename or an mtime is wrong —
  abort rather than rebuild.
* **Caveat on checkpoints:** a dry run before a `checkpoint` rule has produced its
  manifests *cannot* list the jobs behind it. The gate can prove old work is not
  being redone; it cannot size the new work. Do not read a small dry-run job count
  as "nothing much to do".

### Fail loudly on an empty result

The same blindness applies downstream. A merge step that concatenates per-unit
outputs will happily write an empty artifact when *every* unit failed, and an
empty artifact flows into reports as though it were a finding. Make the merge
refuse:

```python
if not frames:
    raise SystemExit(
        f"REFUSING to write an empty {OUT.name}: {n_parts} part files exist and "
        f"every one is empty. A stage that computed nothing is a failure, not a "
        f"result.")
```

This is not hypothetical: an unrooted-tree incompatibility produced 1,652 empty
per-family outputs, and the empty merged table reached a comparison report before
anyone noticed.

### Retrieval

Return files as **declared job outputs** (one archive, one approval), never
per-file transfers — those raise an approval card *each*, and a loop over them
will jam the channel. Ship code up freely (hundreds of KB), harvest final
artifacts down freely (single-digit MB), compute every intermediate summary
remotely and return text. When the question is "is this the file I think it is"
rather than "what is in it", compare checksums in place instead of transferring.

## Make it a test, not a habit

Every check here is cheap enough to run in CI. A rule that reserves more than one
core without passing `{threads}` should fail the test suite, not the fairshare
budget. `check_allocations` returns a list of dicts precisely so it can be
asserted empty:

```python
def test_every_rule_can_use_its_cores():
    assert check_allocations("Snakefile") == []
```
