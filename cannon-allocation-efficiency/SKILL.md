---
name: cannon-allocation-efficiency
description: "Verify that cluster jobs can actually saturate the CPU cores and memory they reserve — before submitting and after running. Use when dispatching Snakemake or SLURM work to a shared HPC cluster, when a rule reserves multiple cores or tens of GB, when a stage runs far slower than its compute would suggest, or when auditing fairshare/core-hour usage. Covers: passing {threads} through to the command, worker pools that cannot fill their slices, libraries that grab every visible core inside an already-parallel loop, batch granularity against heavy-tailed per-unit cost, reserved-vs-used auditing from sacct and snakemake benchmark records, and SLURM job arrays (`sbatch --array`, `%N` throttles, MaxArraySize/MaxSubmitJobs limits, per-task (jobid_taskid) sacct accounting, and why Snakemake's `--slurm-array-jobs` is unsafe in executor plugin 2.8.0), plus publishing a redacted public mirror of a cluster's notes file after every edit."
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
`audit_sacct(text)` does the folding (and handles array tasks); pass it `sacct -P`
output *with* the header line.

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

Two traps in reading it. `TotalCPU` is **zero on the allocation row under `sacct -X`** —
it lives on the `.batch` / `.0` sub-steps (in a full listing, SLURM 26.05 also writes their
sum on the allocation row), so aggregate per job id and take the larger of the allocation
row and the step sum, as `parse_sacct` does, rather than reading one row. And `TotalCPU` is **not populated while a job runs**; for a live job use
`sstat -j <jobid>.0 --format=AveCPU,NTasks,AveRSS` instead.

### Attributing a job to a rule when the job name is a UUID

Snakemake's SLURM executor names every job after the *workflow* uuid, so `sacct` alone
cannot tell you which rule wasted the time — every row carries the same name. The rule is
in the log path the executor writes:

```
.snakemake/slurm_logs/rule_<rulename>/<wildcards>/<jobid>.log
```

Walk that tree to build `rule -> [jobid]`, then aggregate `sacct` per rule. Array tasks
break the pattern: their logs are `.snakemake/slurm_logs/rule_<rulename>/<arrayid>_<task>.log`
with no wildcard directory. Simpler on CANNON, where `AccountingStoreFlags` includes
`job_comment`: the executor writes `rule_<rulename>[_wildcards_<w>]` into `--comment`, so
`sacct --format=...,Comment` carries the rule, and `audit_sacct(text, by="rule")` groups on
it. For an array the wildcards in the comment are the *first task's* only. Rank rules by
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

## Job arrays

**An array saves scheduler work, not core-hours.** `sbatch --array` puts N tasks under
one pending record, but once a task starts it is its own allocation, reserved and
charged exactly like a separate job, and each task still pays its own start-up. Use an
array for many independent units of the same resource shape, launched from your own
sbatch script. Every task gets the *same* request, so size it to the largest unit and
read the spread afterwards: a heavy tail of unit cost or memory is paid by every task.

### CANNON limits (measured 2026-10-21, SLURM 26.05.4)

| limit | value | consequence |
|---|---|---|
| `MaxArraySize` | 10000 | caps the task **index**: `--array=0-9999` is accepted, `--array=1-10000` is rejected ("Invalid job array specification") |
| user association `MaxSubmitJobs` | 10100 | **every task counts**: one 10000-task array is nearly the whole budget |
| `test` QOS | 5 submitted / 5 running | a 5-task array is accepted, a 6-task array fails with `QOSMaxSubmitJobPerUserLimit` |
| `MaxJobCount` | 300000 | cluster-wide |

```python
array_chunks(25000)                    # [{'offset':0,'n':10000,'spec':'0-9999'}, ... offset 20000, '0-4999']
array_chunks(12, throttle=4, max_submit=5)
# in the job script:  UNIT=$((OFFSET + SLURM_ARRAY_TASK_ID))
```

If `n_units > max_submit`, the chunks cannot all be queued at once. Submit the next one only
after the previous one drains; a `--dependency` does not help, because a pending dependent
still counts as submitted.

### Throttle with `%N`, adjust it live

`--array=1-600%50` holds at most 50 tasks running. `scontrol update JobId=<arrayid>
ArrayTaskThrottle=<N>` changes the cap on a live array (verified: `%2` held 2 running,
raising it to 3 started a third). As with any concurrency cap, a throttle costs wall
time, never core-hours. Use one when the tasks hammer shared storage. Lustre small-file
load is an aggregate problem, and a throttle is how one user stays out of it.

### Reading array accounting

* Task rows are `<arrayid>_<task>`, steps `<arrayid>_<task>.batch` / `.extern` / `.0`; a
  not-yet-started remainder is one row `<arrayid>_[3-6%2]` in both `squeue` and `sacct`.
* **`JobIDRaw` is a different number for every task**, and the *last-dispatched* task
  inherits the array's own id as its raw id (task 6 of 54337667 had raw id 54337667; task
  1 had 54337718). `SLURM_JOB_ID` inside a task is that raw id; `SLURM_ARRAY_JOB_ID`
  and `SLURM_ARRAY_TASK_ID` identify the task. Key every join on `JobID`, never on
  `JobIDRaw`.
* `sacct -j <arrayid>` returns every task; `sacct -j <arrayid>_<task>` one task.
* Same folding as for plain jobs: `MaxRSS` and (under `-X`) `TotalCPU` live on steps.

```python
txt = subprocess.run(["sacct", "-j", ids, "-P", "--format",
                      "JobID,JobIDRaw,JobName,Partition,AllocCPUS,ReqMem,Elapsed,"
                      "TotalCPU,MaxRSS,State,ExitCode,Comment"],
                     capture_output=True, text=True).stdout
audit_sacct(txt)               # one row per task: util, waste_core_h, serial_signature
audit_sacct(txt, by="array")   # per array: tail_ratio, mem_headroom, failed, serial_units
```

Read `by="array"` for what one shared request costs. `tail_ratio` is max/median elapsed;
well above 1 means the slow units set the walltime every task reserved. `mem_headroom`
is request / peak RSS across tasks. `serial_units` counts tasks at roughly 1/AllocCPUS
utilisation, which is the Job Defense Shield pattern, task by task.

### Snakemake `--slurm-array-jobs`: do not enable it with executor plugin 2.8.0

The plugin can submit a rule's ready jobs as one array (`--slurm-array-jobs=rule1,rule2`
or `all`; `--slurm-array-limit`, default 1000, capped at `MaxArraySize - 1`). Tested on
CANNON 2026-10-21 with the versions in the project envs (snakemake 9.25.1,
snakemake-executor-plugin-slurm 2.8.0, -jobstep 0.6.1), on a toy rule of 12 two-core
jobs, it was not safe. Each of the following was reproduced:

1. **Every task re-targets the first task's job.** Each task is submitted with the
   chunk's first job as its base command, and runs its own job nested inside that. The
   outer Snakemake process therefore *deletes the first job's output on start-up*
   (`Job.prepare()` removes existing outputs) and *requires it at exit*. A task that
   starts after task 1 has finished wipes task 1's output. A task that checks before
   task 1 has written fails with `MissingOutputException`, even though its own job
   succeeded. Some chunks passed by luck of timing. In one run, two tasks started a
   minute after the others, once task 1 had finished; 7 of the chunk's other 9 tasks
   then failed on task 1's missing output. In another run, 5 of 6 tasks of a chunk
   failed. This is upstream
   issue #488. PR #489 (opened 2026-10-01) proposes the fix; the 2.8.0 release
   (2026-08-10) predates both.
2. **Under the default `sacct` status query, every task reports one task's state.** The
   plugin queries `sacct -X --format=JobIdRaw,State`. Task ids `<A>_<t>` never appear in
   that output, so the lookup falls back to `<A>`, the raw id of the last-dispatched task. One
   task that failed in 2 s got 10 running siblings marked FAILED, and `--retries 1`
   resubmitted all 11 while the originals were still running: 22 executions for 11
   units, with duplicate jobs writing the same outputs. If the base task finishes
   *first*, a still-running sibling is declared done instead. `--slurm-status-command squeue` reports `<A>_<t>` correctly
   (`MinJobAge` is 600 s on CANNON, enough for squeue to still see finished tasks), and
   fixes this item only.
3. **`--jobs` below half the ready jobs deadlocks.** The scheduler marks the selected jobs
   as running before handing them over. The plugin then holds them back ("Array job
   collection incomplete … waiting for more jobs") whenever they number fewer than the
   rule's still-ready jobs and fewer than the chunk size. They are never submitted, and
   never offered again. Verified: 12 ready with `-j 4`, no sbatch call ever made, and
   SIGTERM only produced "Will exit after finishing currently running jobs", so the driver
   sat until its walltime ran out. With `-j 6` the same 12 went out as two arrays of 6.
   `array_mode_stalls(ready, jobs)` encodes the condition.
4. **All tasks get the first job's resources**, plus a ≥1 MB payload fudge on `--mem`.
   A unit declaring `mem_mb=2100` ran with 2001. Callable or `attempt`-scaled resources
   are silently under- or over-reserved. `check_allocations(path, array_rules=[...])`
   flags them, along with `group:` rules, which are never arrayed.
5. **Task 1 runs without the srun step.** The executor's efficiency report (built from
   `.N` step rows) omits the first task of every array, and in that task's shell
   `SLURM_CPUS_PER_TASK` and `SLURM_MEM_PER_NODE` are unset. Any `-t
   ${SLURM_CPUS_PER_TASK}` idiom breaks there; pass `{threads}`. Its CPU time is still on
   `.batch`, which `audit_sacct` counts.
6. **Arrays form only from jobs that become ready in the same scheduling pass.** When
   upstream jobs finished one at a time, all six units went out as regular jobs. The
   documented "synchronization overhead" did not appear, but neither did any saving.

Leave array mode off. If scheduler load is the concern, cap `--jobs` and re-granulate the
rule so that one job processes a batch of units (see "After it finishes"), then verify
the batch actually fills its cores with `audit_sacct(by="rule")`. Before relying on
`group-components` for that, check utilisation: upstream issue #495 reports grouped
components on 2.8.0 running one at a time inside an allocation sized for all of them in
parallel. That has not been checked on CANNON.

**Re-test before enabling on a newer plugin.** `array_probe/Snakefile` in this skill is the
toy workflow used above. Three runs cover the failures: `--config failone=1` (status
isolation and retries), prebuilt `prep/` with `-j 4` (stall), and `-j 6` with a busy
queue (first-task output race). A fixed plugin passes when every run
finishes on its own, and the task count in `audit_sacct(txt, by="unit")` equals the
number of units, plus the single retry of unit 3 under `failone=1`, with no other FAILED
task. `check_allocations("array_probe/Snakefile", array_rules=["unit"])` flags the
probe's per-unit `mem_mb` on purpose (item 4).

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

## Publishing the cluster notes

A cluster's notes file — partitions, reservations, scheduler quirks — is more useful
shared, but it accumulates usernames, lab paths and SSH details. Publish it only through
`publish_cluster_notes`, which redacts first and refuses rather than leaking:

* usernames (from `/n/homeNN/<user>`, `/Users/<user>`) become `$USER`, home paths
  `$HOME`, lab names (from `/n/<vol>/LABS/<lab>`, `/n/netscratch/<lab>`) `<lab>`. All
  are derived from the text, so nothing identifying is hard-coded here;
* any section mentioning `~/.ssh/`, `IdentityFile`, deploy keys or private keys is
  dropped whole;
* the private rule block between `<!-- SYNC-RULE:BEGIN` and `SYNC-RULE:END -->` is
  stripped. It may carry extra lines `redact: TERM -> REPLACEMENT`;
* it raises if any derived or extra identifier survives (case-insensitive) or a
  token/key pattern appears, and it commits only when the redacted text changed.

When the notes carry a SYNC-RULE block, **every edit to them is followed, in the same turn,
by these three steps** — the block names the repo, path and title:

1. Read the notes after your edit: `compute_details(provider=..., mode="read")`.
2. In `repl`, lift that read result out of this session's transcript into a file. Do not
   retype it — 20 KB copied by hand is not the same 20 KB:

```python
import json, os
def latest_notes_read(frame_id, provider):
    n = host.frames(frame_id=frame_id, max_results=1)["total_messages"]
    off, uses, best = max(0, n - 60), set(), None
    while True:
        d = host.frames(frame_id=frame_id, offset=off, max_results=60, include_tool_results=True)
        for m in d["messages"]:
            for b in (m.get("content") if isinstance(m.get("content"), list) else []):
                inp = b.get("input") or {}
                if (b.get("type") == "tool_use" and b.get("name") == "compute_details"
                        and inp.get("mode") == "read" and inp.get("provider") == provider):
                    uses.add(b["id"])
                if b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                    c = b.get("content")
                    t = c if isinstance(c, str) else "".join(x.get("text", "") for x in c if isinstance(x, dict))
                    try:
                        best = json.loads(t)["details"]
                    except Exception:
                        pass
        if not d.get("truncated") or off + len(d["messages"]) >= n:
            return best
        off += len(d["messages"])

txt = latest_notes_read("<this session's Frame ID>", "ssh:cannon")
assert txt, "no compute_details read found in this session - do step 1 first"
os.makedirs("handoff", exist_ok=True)
open("handoff/cluster_notes.md.tmp", "w").write(txt)
os.replace("handoff/cluster_notes.md.tmp", "handoff/cluster_notes.md")
```

3. In `python`, with the GitHub credential declared on the cell:

```python
r = publish_cluster_notes("handoff/cluster_notes.md", repo="<owner/name>",
                          dest_path="<path in repo>", title="<title>")
print(r["status"], r.get("commit"), r["report"])   # 'committed' | 'unchanged'
```

Read `report["dropped_sections"]` before moving on. A section you meant to share that
got dropped mentions SSH material; reword it in the notes instead of weakening the
pattern. Run with `dry_run=True` to inspect `r["text"]` without pushing. A refusal is
the check working: fix the notes, or add a `redact:` line to the rule block, and rerun.
Never hand-edit the published copy, because the next sync overwrites it.

This is agent-side by design. Nothing fires when someone edits the notes in a
settings page, and an agent sandbox cannot watch the app's own storage. Edits made there
go public at the next agent edit, or when someone asks for a sync.

## Make it a test, not a habit

Every check here is cheap enough to run in CI. A rule that reserves more than one
core without passing `{threads}` should fail the test suite, not the fairshare
budget. `check_allocations` returns a list of dicts precisely so it can be
asserted empty:

```python
def test_every_rule_can_use_its_cores():
    assert check_allocations("Snakefile") == []
```
