# Running a workflow: local, cluster, cloud

Verified against Snakemake 9.27. The big structural change to know: **v8
replaced the `--cluster` command-string interface with executor plugins.** Any
example showing `--cluster "sbatch ..."` predates it.

## Contents

- [Local](#local)
- [Software deployment](#software-deployment)
- [Profiles](#profiles)
- [Executor plugins](#executor-plugins)
- [Grouping and batching jobs](#grouping-and-batching-jobs)
- [Failure handling](#failure-handling)
- [Storage plugins](#storage-plugins)
- [Handing off to the allocation audit](#handing-off-to-the-allocation-audit)

## Local

```bash
snakemake --cores all --sdm conda
```

`--cores`/`-c` is the total core budget Snakemake schedules against (`-j` is an
alias for it locally, though upstream recommends `--cores` there; remotely `-j`
means something else entirely, see below). It reads each rule's `threads` to decide how many jobs fit at once, so
**a workflow with no `threads` declarations runs one job per core regardless of
what those jobs actually need**, which either underuses the machine or
oversubscribes it.

Declare both sides and pass them through:

```python
rule align:
    threads: 8
    resources:
        mem_mb=16000,
        runtime=120,
    shell:
        "bwa mem -t {threads} {input} > {output}"
```

`{threads}` reaching the command is the part that's easy to get wrong: a
reservation the tool ignores is a lie to the scheduler, local or remote. The
same applies in reverse — a library that grabs every visible core inside an
already-parallel Snakemake run oversubscribes the machine; cap it explicitly
(`OMP_NUM_THREADS={threads}` and friends).

Override without editing rules: `--set-threads rule=N`,
`--set-resources rule:mem_mb=N`, and `--default-resources` for a baseline
applied to every rule.

## Software deployment

`--software-deployment-method` (aliases `--deployment-method`, `--deployment`,
`--sdm`) takes any of `conda`, `apptainer`, `env-modules`. This generalizes
`--use-conda`, which still works.

- `--sdm conda` — one env per rule from `workflow/envs/*.yaml`. The default and
  the right choice for most analysis workflows.
- `--sdm apptainer` — run rules inside the `container:` each declares.
- `--sdm conda apptainer` — conda envs created *inside* a container, which is
  the most reproducible combination and what `--containerize` targets.
- `--conda-create-envs-only` — build every env and stop. Do this first on a
  machine with slow or rate-limited network, so a long run doesn't fail an hour
  in on a solver timeout.

Envs are cached per-workflow under `.snakemake/conda`, keyed by the env file's
content — editing an env file creates a new env and reruns its rules (the
`software-env` rerun trigger).

## Profiles

Two kinds, and conflating them is why profiles get copied between machines and
break:

| flag | scope | holds |
|---|---|---|
| `--workflow-profile` | this **workflow** | per-rule resources, default targets — things true wherever it runs |
| `--profile` | this **compute environment** | which executor, job limits, cluster-specific paths |

A workflow ships the first; the site supplies the second. Snakemake picks up
`profiles/default/config.yaml` from the working directory automatically, which
is convenient until you're debugging and need it out of the way —
`--workflow-profile none` bypasses it.

Profile entries are the YAML form of CLI flags: `set-resources: align: mem_mb=8000`
becomes `--set-resources align:mem_mb=8000`. A `profiles/default/config.yaml`
carrying `software-deployment-method: conda` and `cores: all` is what lets the
documented invocation be a bare `snakemake`.

## Executor plugins

Builtin `--executor` choices are only `local`, `dryrun` and `touch`. Everything
else is a plugin named `snakemake_executor_<name>`, installed separately:

```bash
pip install snakemake-executor-plugin-slurm
snakemake --executor slurm -j 100 --default-resources slurm_account=... slurm_partition=...
```

Plugins exist for SLURM, generic cluster submission, Kubernetes, Google Life
Sciences, AWS Batch, Azure Batch and others. Each adds its own flags (SLURM's
are `slurm_account`, `slurm_partition`, `slurm_extra`, ...), set per-rule via
`resources:` or globally via `--default-resources`.

`-j`/`--jobs` means something different remotely: at most N cluster/cloud jobs
in parallel, not cores (`--cores` there caps the cores requested from the
scheduler). Set it to what the queue tolerates, and keep `threads` and
`resources` accurate so each job's reservation is right.

## Grouping and batching jobs

A workflow with thousands of short jobs spends more time in queue latency than
in compute. Two mechanisms:

- `group: "somename"` on several rules makes their jobs submit as one unit, so a
  chain of quick steps per sample becomes one cluster job.
- `--group-components somename=10` packs ten instances of that group into one
  submission.

This is a real speedup on a busy scheduler and costs nothing locally. The
tradeoff is granularity: a grouped job that fails takes its whole group with it,
and one long-tailed member holds the group's reservation.

## Failure handling

| flag | use |
|---|---|
| `--keep-going` (`-k`) | finish independent branches rather than stopping at the first failure. Applies only to runtime failures in job execution, not to parsing or DAG-construction errors |
| `--retries N` | retry a failed job N times — for flaky I/O, not for deterministic failures |
| `retries: 3` (per rule) | same, scoped to the rule that actually needs it |
| `--rerun-incomplete` | redo jobs whose outputs were left partial by a killed run |
| `--unlock` | release the lock after a killed run |

`resources: mem_mb=lambda wildcards, attempt: attempt * 8000` scales a
reservation on each retry, which is the idiomatic answer to a job that
occasionally needs more memory than the median.

Set `benchmark: "benchmarks/{sample}.tsv"` on the expensive rules. It records
wall time and peak memory per job, which is the only honest basis for setting
`resources` — and it's what an allocation audit reads later.

## Storage plugins

v8+ moved remote file access to storage plugins
(`snakemake-storage-plugin-s3`, `-gcs`, `-http`, ...). Declare a provider and
wrap paths in `storage(...)`, rather than the old per-protocol `remote()`
classes. Relevant when inputs live in object storage; not needed for a workflow
on local or shared filesystem.

## Handing off to the allocation audit

This document covers how to *declare* compute. Whether the declaration is
honest — whether jobs saturate the cores they reserve, whether batch
granularity suits a heavy-tailed cost distribution, what `sacct` says you
actually used against what you asked for — is a separate question with its own
skill.

**When a workflow goes to a shared cluster, load
`cannon-allocation-efficiency`.** Especially before requesting many cores per
job or tens of GB, and whenever a stage runs far slower than its reservation
suggests it should.
