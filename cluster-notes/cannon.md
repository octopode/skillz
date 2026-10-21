# CANNON (Harvard FASRC) cluster notes

> Mirrored automatically from the maintainer's working notes, with personal identifiers replaced by `$USER`, `$HOME` and `<lab>`. Edits made here are overwritten; open an issue instead.

# ssh:cannon · ssh

## Settings
scratch_root: $HOME/.claude-science-scratch  # workdirs & uploads only — c.download() takes any host path

## Resources
cpus: 32
gpus: 28
mem_mb: 1031076
scheduler: slurm
package_managers: conda, module, apptainer

## Slurm
partitions: bigmem, bigmem_intermediate, gpu, gpu_h200, gpu_requeue, gpu_test, intermediate, sapphire, serial_requeue, shared, test, unrestricted

## User-provided context
This is Harvard FASRC's CANNON cluster. Its complete documentation (including partition table) is found at https://docs.rc.fas.harvard.edu. Live cluster status is at https://fasrc.instatus.com. Queueing is handled by slurm. My work exclusively involves DSL1/DSL2 data, so CANNON's data security policy is not an issue.

Job bodies run under `bash -eo pipefail ./cmd.sh` (set by the harness wrapper in job.sh), so `set -e` is in force no matter what the submitted body says — writing `set -uo pipefail` at the top does NOT clear it. A diagnostic job that runs several steps and reports each exit code must start its body with an explicit `set +e`, or the first failing command (including the first failing *pipeline*, e.g. `pytest ... | tail`) silently truncates the rest of the job. Symptom: slurm-*.out ends mid-script with no error, sacct shows FAILED 1:0, and the later `echo` section headers never appear.

Scheduling long driver jobs: check `scontrol show reservation` BEFORE choosing a walltime. Slurm will not start a job whose walltime would overlap a MAINT reservation, and the only symptom is `squeue` showing `(ReqNodeNotAvail, Reserved for maintenance)` with no start-time estimate — it looks like ordinary queue pressure but waiting does not help. Size the request to fit in front of the window and have the script resubmit itself (snakemake is resumable, so a slice boundary costs only the next queue wait). Maintenance reservations here have covered ~379 nodes / 41.5k cores at a time, including most of sapphire's holy8a hardware and the holygpu8a GPUs, for multi-day windows.

Partition notes for a small long-lived driver process (1-2 cores, ~16 GB, must outlive a multi-day cascade): `unrestricted` has a 365-day cap but only 8 nodes and is routinely fully allocated with dozens queued; `intermediate` (14 days, 12 nodes) has had 800+ jobs pending. `sapphire` (3-day cap, 184 nodes) starts such a job in under a minute, so running the driver in sub-3-day self-resubmitting slices beats waiting for a long-cap partition. `bigmem_intermediate` cannot be used as a fallback at all: sbatch rejects any job requesting under 1000 GB per node ("You must request more than 1000GB per node for jobs in bigmem_intermediate partition").

The snakemake slurm executor prints "No SLURM account given, trying to guess" and then "sacct: invalid option -- '1'" before proceeding without an account. This is benign here — jobs submit and run fine — but it means every rule's job carries no explicit account.

Snakemake resource precedence, and how it interacts with maintenance windows. A workflow profile's `set-resources:` block OVERRIDES the per-rule `resources:` written in the Snakefile. Editing the Snakefile alone changes nothing and gives no warning — a rule whose Snakefile line read `runtime=1440` was still submitted with `TimeLimit=2-00:00:00` because the profile set 2880. Change wall-time and memory in the profile, and verify against slurm itself (`scontrol show job <id> | grep TimeLimit`), never by reading the Snakefile.

This matters more here than it would elsewhere, because slurm refuses to start a job whose wall limit would overlap a MAINT reservation. An oversized limit therefore does not merely waste a node slot — it parks the entire stage until after a multi-day outage, showing only `(ReqNodeNotAvail, Reserved for maintenance)` in squeue. Diagnose by reading each rule's recorded benchmark (`logs/bench/<rule>/*.tsv` holds wall seconds in column 1) and comparing with the submitted TimeLimit; reservations at 2x the measured maximum schedule fine and still leave headroom. `restart-times` in the profile covers the rare overrun.

scancel on a large pending set prints many `_add_delay: adding delay in RPC send` lines and takes ~30 s for ~70 jobs; that is RPC backoff, not an error.

STATUS PAGE — check this FIRST when jobs fail strangely, before theorising.
https://fasrc.instatus.com (plain HTTPS GET of `/` works; the instatus JSON endpoints /summary.json and /v2/summary.json both return 403, so parse the rendered HTML). It carries a per-component table (each storage volume and login/compute service listed Operational / Degraded performance / Down), an incident feed, and scheduled-maintenance entries with their affected partitions. Monthly maintenance is the first Monday of the month except holidays; larger multi-day cooling/power work is announced as its own scheduled entry.

Read the component table, not just the incident headlines, and match the volume name to the one your project is actually on — `df -h --output=target <path>` and `stat -f -c '%T' <path>`. The volumes are separately reported and a degradation on one says nothing about another: as of 2026-09-28 HolyLFS06 (Tier 0) was "Degraded performance" with an incident open since 2026-09-18, while BosLFS02 (Tier 0) — where the <lab> project trees live — was Operational the whole time. Attributing a job failure to the wrong volume's incident is an easy and misleading mistake.

The standing FASRC guidance from that incident is still worth following regardless of which volume you are on: groups with large sequential reads or small-file workloads should move that traffic to `/n/netscratch`. Their stated reason is aggregate load rather than any one job, and the failure mode they describe is slow journal commits leading to service threads stuck in uninterruptible wait, so a workload that creates thousands of tiny files in one directory is the kind that hurts. `/n/netscratch` is NFS, not Lustre (`stat -f` confirms), and is already mounted in the <lab> trees as `<project>/work`. Job-efficiency guidance: https://docs.rc.fas.harvard.edu/kb/job-efficiency-and-optimization-best-practices/

Corollary for snakemake drivers: do NOT relaunch a driver while its jobs are still in flight. Snakemake deletes the outputs of jobs it is about to re-run, so a relaunch with --rerun-incomplete races any job the previous driver left running — the new driver clears a flag directory that an orphaned job then writes into, or writes a flag the new driver has just decided to remove. Drain first: scancel the driver, then poll until `squeue -u $USER` is empty, and only then relaunch.

RESERVATIONS ARE PER NODE FAMILY, NOT CLUSTER-WIDE. This is the single most useful thing to know when `squeue` shows `(ReqNodeNotAvail, Reserved for maintenance)`. Map the reservation to its nodes and the partitions to theirs, then check whether they actually intersect:
  scontrol show reservation | grep -oE 'ReservationName=[^ ]+|Nodes=[a-z0-9]+' | paste - -
  for p in sapphire test shared unrestricted intermediate; do printf '%-14s %s\n' "$p" "$(sinfo -h -p $p -o '%N' | head -c 60)"; done
Observed 2026-09-29: reservation `cdu6` covered holy8a, which backs **sapphire, test and intermediate**; reservation `powerwork2` covered holy7c, which backs **shared and unrestricted**. During cdu6 the sapphire/test/intermediate jobs were unschedulable for four days while `shared` was running 361 of its 370 nodes for other groups. Anything pinned to `test` — cheap planner rules are a common case — is blocked by a holy8a reservation even though it needs seconds of compute; list `test,shared` so a reservation on either family leaves it somewhere to run. Sizing a wall limit against a reservation that does not touch your partitions costs days of waiting for nothing.

NEVER RUN `scancel -u $USER` FROM INSIDE A SLURM JOB — it cancels the job running it. This bit twice in one day: a driver's self-resubmit wiped the driver, and a maintenance job cancelled itself halfway through editing files. Skip your own id: `for j in $(squeue -u $USER -h -o '%i'); do [ "$j" = "${SLURM_JOB_ID:-}" ] && continue; scancel "$j"; done`.

NEVER EDIT A SHELL SCRIPT THAT A RUNNING JOB IS EXECUTING. Bash reads a script incrementally by byte offset, so inserting lines into a long-running driver's script shifts what the live shell reads next. Editing `rerun_cannon.sbatch` at 10:40 while a driver started at 10:23 was still running meant that at 21:45 its self-resubmit executed the pre-edit form of the call and cancelled the driver. Drain, confirm `squeue` is empty, edit, then relaunch.

### `test` cannot appear in a multipartition submission

FASRC's slurm rejects any `sbatch` whose partition list contains `test`
alongside another partition:

```
sbatch: error: test cannot be included in multipartition submissions
sbatch: error: Batch job submission failed: Unspecified error
```

`--partition=test` alone is fine; `--partition=test,shared` is not. This
bites hardest through a snakemake profile, because a `set-resources:` entry
like `plan_matrices: slurm_partition=test,shared` makes that rule
permanently unsubmittable, and snakemake surfaces it only as

```
Error in rule plan_matrices:
    message: SLURM sbatch failed. ...
    shell: (command exited with non-zero exit code)
```

with no slurm log and no `sacct` record for the rule — because the job was
never created. If a rule fails instantly, repeatedly, and leaves no
`.snakemake/slurm_logs/rule_<name>/*.log` and no sacct row, suspect a
submission-time rejection and grep the driver log for `sbatch: error`.

`test` is on holy8a, so the tempting pattern during a holy8a reservation —
pairing it with a holy7c partition so short jobs can land on whichever
family is free — is exactly the pattern slurm forbids. Use
`shared,sapphire` (both combinable) and accept that during a holy8a
reservation the sapphire member simply never matches.

### Keeping a signal on a long login-node driver: the heartbeat watcher

A snakemake driver launched the sensible way — `nohup`/`sbatch` from the
login node, so no allocation sits idle — produces **no `compute_done`
notification**, because the session never dispatched it. There is then
nothing to park on: `wait_for_notification` returns `status: 'error'`
immediately when no job of yours is in flight. It is very easy to launch a
multi-day cascade and then simply stop looking at it.

Bind a signal to it with a cheap watcher submitted through `submit_job`,
which *does* notify:

```bash
#SBATCH --partition=shared
#SBATCH --time=75
#SBATCH --mem=2000
#SBATCH --cpus-per-task=1
DRIVER=<driver jobid>
END=$(( $(date +%s) + 3600 ))
REASON=interval
while [ "$(date +%s)" -lt "$END" ]; do
  squeue -h -j $DRIVER -o '%i' | grep -q . || { REASON=driver_gone; break; }
  sleep 60
done
echo "RESULT woke_because=$REASON"
# ... then a progress digest: sacct state, child job count, output counts,
#     `grep -c '^Error in rule'`, and a filtered log tail
```

It returns on whichever comes first: the driver leaving the queue, or the
interval. The early exit is the valuable half — `rerun_cannon.sbatch`
resubmits itself only on `timeout`'s rc 124, so an unrecoverable rule
failure makes it print "NOT resubmitting, needs a look" and stop
**indefinitely**. Without a watcher that silence is indistinguishable from
healthy progress.

Cost is 1 core-hour per interval, against a cascade budget in the hundreds.
Re-submit a fresh watcher each time one lands and the driver is still up;
60 min is a reasonable interval for a multi-day run, 30 min while a slice is
still proving itself. Set `run_timeout_s` above the interval (the host clock
counts queue time too).

Pair it with a partition whose limit exceeds the interval, and never let the
watcher mutate anything — no `scancel`, no edits to files the driver reads.

### Submitted job bodies run under `set -e`

The host's job wrapper enables `set -e`, so any command that exits non-zero
aborts the whole script — including the ones you do not think of as failing.
A `grep` that finds no match returns 1 and kills the job mid-way; so does
`[ -f x ]` on a missing file, or the last stage of a pipeline you only wanted
to peek at. The symptom is a job that ends with exit 1 and a truncated log
whose last line is a successful-looking `echo`, with every later section simply
absent — it looks like the job was killed, but it was a non-matching grep.

Write diagnostic sections defensively: `set +e` at the top of the body, or
append `|| true` to every grep/test used as a probe rather than a gate. Keep
the real gates unguarded so they still fail the job.

### Always drive snakemake through `scripts/snake_cannon.sh`

Do not call `snakemake --profile profiles/cannon ...` directly from a submitted
job. The repo has a `profiles/default/` directory, and snakemake auto-loads it
as the WORKFLOW profile whenever it exists; it contains `cores: 4`, which clamps
every rule's `threads:` to 4 no matter what the rule or `profiles/cannon`
declares. `snake_cannon.sh` exists precisely to pass `--workflow-profile none`
(plus `--profile profiles/cannon` and `--cores`), and `rerun_cannon.sbatch` uses
it. A driver that bypasses it silently bills 16-thread rules as 4-core jobs, and
the symptom looks exactly like a misconfigured profile.

Note also that `set-resources:` in `profiles/cannon/config.yaml` OVERRIDES the
Snakefile for the rules it names (phylo_d_batch, matrix_batch,
extract_family_seqs and others). Changing a rule's `runtime:` or `mem_mb` in the
Snakefile alone will not take effect for those rules — grep the profile first.

### Size the driver job at 1 core / 2 GB

The snakemake driver submits children and polls; it does no compute. Measured
over a three-track sweep, 13 drivers reserved 91.3 core-h and used 2.7 (2.9%),
peaking at 1.7 GB against 198.7 GB reserved — the two worst jobs by wasted
core-hours in the whole window were drivers. Use `--cpus-per-task=1 --mem=2G`
and spend the budget on `--time` instead.

### Measured per-rule fill (2026-10, after the contamination rerun)

Cores actually used (CPU-time / wall) when each rule held 4 cores:
`phylo_d_batch` 1.78, `ml_tree_batch` 1.43, `site_batch` 1.92, `stage6_batch`
3.51, `matrix_batch` 4.61 (i.e. 115% — oversubscribed). Peak RSS: phylo_d 5.7 GB,
ml_tree 2.2 GB, site 1.1 GB, stage6 2.3 GB, matrix 9.8 GB. The pooled phylo
rules are tail-limited — a batch ends when its slowest family ends and tree cost
is superlinear in tip count — so adding cores does not help; smaller batches
would. `cannon_env.sh` now pins OMP/OPENBLAS/MKL to 1 thread, because pool
workers were each spawning BLAS threads on top of the pool.

### sbatch can fail at submit under cluster load

`sbatch: error: Unable to open file assigned` appeared 242 times on one track
while the cluster held ~17k queued jobs, and zero times on two tracks run
earlier. No job is created and there is no sacct row, so it only shows up in the
driver log. `restart-times: 2` in the profile absorbs most of them; treat a
burst as scheduler contention rather than a workflow fault.

### graphviz and staging notes (verified 2026-10-19)

`dot` is at `/usr/bin/dot` (graphviz 2.40.1) on compute nodes and also in `envs/prefixes/pa-flow/bin/dot`; no `module avail graphviz`. Tarballs built on macOS and unpacked on CANNON drop AppleDouble `._*` files (and `tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr...'` warnings) next to every extracted file — remove them before `git status` is read. Snakemake 9.x has no `--reason` flag (reasons print by default under `-n`); passing it exits 2 before the DAG is built.

### BosLFS02 can stall cluster-wide while the status page says Operational (observed 2026-10-19)

From ~13:24 to 15:22 EDT every access under `/n/boslfs02` (even `ls /n/boslfs02`) hung on holylogin06 and on compute nodes in both families (holy8a, holy7c); fasrc.instatus.com listed "BosLFS02 (Tier 0)" OPERATIONAL throughout. Symptom from the harness: `call_command` hits its 60 s cap with no output. Probe with `timeout 20 ls -d <path>; echo rc=$?` (124 = hung) rather than bare commands, and don't submit jobs that touch the tree until it returns — a job that `cd`s in can then block for its whole walltime in uninterruptible I/O. It recovered without intervention; netscratch (`/n/netscratch`) and home stayed usable during the stall.

### `c.download()` of a project-tree path hangs (observed again 2026-10-19)

`c.download('/n/boslfs02/LABS/<lab>/.../<file>')` on small text files (4-9 KB) did not
return after 20+ minutes and had to be abandoned, leaving the repl kernel busy (use
`fresh: true` on a `repl` call to keep working, and `host.compute.create(...)` again there to
`close()`). Reliable alternative: copy the file into a submitted job's workdir and name it in
`outputs=`, or — for anything already committed — read the bytes from GitHub and verify them
against `git rev-parse HEAD:<path>` (the blob sha is a byte-identity check).

### SLURM job arrays (verified 2026-10-21, slurm 26.05.4)

MaxArraySize=10000 bounds the task INDEX (`--array=0-9999` ok, `1-10000` rejected). Every array task counts against MaxSubmitJobs (user association <lab>: 10100; `test` QOS: 5 — a 6-task array fails with QOSMaxSubmitJobPerUserLimit). `%N` throttles work and `scontrol update JobId=<A> ArrayTaskThrottle=<N>` changes them live. MinJobAge=600 s. AccountingStoreFlags=job_comment,job_script, so sacct `Comment` carries snakemake's `rule_<name>` tag. In sacct, each task has its own JobIDRaw and the last-dispatched task inherits the array id as its raw id — key on JobID.

Do NOT use snakemake `--slurm-array-jobs` with the pa-flow env's executor plugin 2.8.0 / jobstep 0.6.1: tasks delete/require the first task's output (upstream #488), sibling status is mis-mapped under the default sacct status query, and `--jobs` below half the ready jobs deadlocks (SIGTERM does not end it; needs SIGKILL). Details and re-test probe in the cannon-allocation-efficiency skill.

Snakemake 9 `--config` keys must be >= 2 characters (`n=12` -> "Config entry must start with a valid identifier").
