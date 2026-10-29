---
name: snakemake-workflow
description: "Build, extend and review Snakemake workflows to upstream best practice. Use this skill whenever a Snakefile, a .smk rule file, a `rule:` block, or a Snakemake pipeline is in play - adding or refactoring a rule, choosing where a new analysis attaches, wiring conda envs, debugging why jobs rerun or a rule is ambiguous, setting up reports or unit tests, or reviewing someone's workflow for reproducibility. Also use it when a user describes a multi-step file-to-file analysis they want made rerunnable, even if they never say the word Snakemake. Covers the official layout, the --lint/snakefmt/--report/--containerize/--generate-unit-tests toolchain, DAG and wildcard semantics that cause real bugs, and threads/resources for local and cluster execution."
metadata:
  authored_via: "claude-science"
  published_by: "agent"
  published_at: "2026-10-22T21:48:29.637Z"
  authored_by: "unknown"
  last_modified_by: "agent"
  last_modified_at: "2026-10-22T21:35:34.428Z"
---

# Snakemake workflows

Upstream Snakemake practice: the official layout, the checks that catch problems
before a reviewer does, the semantics that cause real bugs, and how to declare
compute so a workflow runs the same on a laptop and a scheduler.

Everything here is verified against **Snakemake 9.27**. Check `snakemake
--version` before trusting a flag — v8 replaced the cluster interface with
executor plugins, and pre-v8 examples on the web still show `--cluster`.

## Where things go

The [official
layout](https://snakemake.readthedocs.io/en/stable/snakefiles/deployment.html)
is worth following even for a workflow nobody else will run, because every
Snakemake user can navigate it without being told, and because `snakedeploy`
and the Snakemake Workflow Catalog assume it:

```
config/config.yaml          every tunable: paths, thresholds, group definitions
config/samples.tsv          sample sheet, if the workflow has one
workflow/Snakefile          entry point: configfile, includes, rule all
workflow/rules/*.smk        rules grouped by stage; common.smk holds helpers
workflow/scripts/*          one script per rule
workflow/envs/*.yaml        one conda env per rule (or per stage)
workflow/schemas/*.yaml     JSON schemas validating config and sample sheet
workflow/report/*.rst       caption files for report(...) outputs
resources/                  operator-supplied inputs (usually untracked)
results/                    all outputs (untracked)
```

Two habits that pay for themselves immediately:

- **No sample name, group name or threshold anywhere in `workflow/`.** Rules read
  the config and derive the rest. This is what makes a new sample add jobs to the
  DAG instead of requiring a code change — and it's the difference between a
  workflow you can hand someone and one only you can run.
- **Every rule gets a `log:`, a `conda:` and a docstring.** The log because a
  failing job in a 200-job run is otherwise unfindable; the env because
  reproducibility is per-rule, not per-repo; the docstring because `--lint`,
  `--report` and any documentation generator read it.

Pin the minimum version in the Snakefile so an old Snakemake fails loudly
instead of misinterpreting your rules:

```python
from snakemake.utils import min_version
min_version("9.0")
```

Validate the config and the sample sheet against JSON schemas rather than
hoping. `validate(config, "../schemas/config.schema.yaml")` — signature is
`validate(data, schema, set_default=True)`, so schema defaults populate missing
keys, which means a config can stay short without the scripts guessing.

## The review gate

Run these before committing a pipeline change, and when reviewing someone
else's. They take seconds and each one catches a distinct class of problem.
Run them in the environment that has Snakemake installed.

```bash
snakemake --lint                    # workflow-level code quality
snakefmt --check workflow/          # formatting (pip install snakefmt)
snakemake -n --use-conda            # does the DAG still build, and what reruns
snakemake --rulegraph | dot -Tpdf -o dag.pdf
```

**`--lint` is the one to actually read.** Upstream recommends it before
publishing a workflow, and its complaints are specific: a rule with no conda
env, no log, an absolute path, a `params`-less `shell` with a hardcoded value.
It takes `--lint json` if you want to gate a commit hook on it. One lint fires
often and is worth understanding rather than silencing: *"Mixed rules and
functions in same snakefile"* means input-function helpers are living beside
rules. Move them to `common.smk` — they run at DAG-build time, after every
`include:`, so a helper there can still reference rules defined in any rule
file even though `common.smk` is included first.

That same lint can fire on a file containing **no rules at all**, because it is
detected by matching `rule .+?:` against the file's *text* rather than its parse
tree. Prose in a module docstring is enough to trip it: the sentence "declaring
the sheet as a rule input would satisfy (a) and violate (b):" fired it on a
pure-helpers file. Reword the sentence if you want a clean gate, and leave a
note saying why, so the next reader doesn't revert it as a typo. Knowing the
lint reads text, not structure, is what lets you tell a real finding from this.

**`-n` is a semantic check, not just a syntax check.** Read *what it plans to
rerun*. If it wants to redo work you thought was done, that's `--rerun-triggers`
telling you something changed — see below. A clean run prints `Nothing to be
done`.

**Pass the deployment flags to the dry run too.** On a workflow that ran under
`--use-conda`, a bare `snakemake -n` reports every `conda:` rule as needing a
rerun, with the reason *"software environment definition has changed since last
execution"* — the environment is not deployed during a dry run, so the hash it
compares against the recorded one does not match, and the `software-env` trigger
fires on a workflow where nothing changed. `snakemake -n --use-conda` on the
same workflow prints `Nothing to be done`. Verified on 9.27.

Then, once the workflow runs:

```bash
snakemake --report report.html      # provenance + runtime + code (needs pygments)
snakemake --containerize > Dockerfile
snakemake --generate-unit-tests .tests/unit
```

For what each of these actually produces, its gotchas, and the inspection flags
(`--summary`, `--detailed-summary`, `--filegraph`, `--list-changes`), read
`references/toolchain.md`.

## The semantics that cause real bugs

These six account for most of the time people lose to Snakemake. Full
explanations and fixes in `references/correctness.md`.

1. **Jobs rerun when the *code* changes, not just the input.** The default
   `--rerun-triggers` is `code input mtime params software-env`. Editing a
   script or a `params` value reruns every job downstream — correct, and
   surprising if you expect `make`. `--rerun-triggers mtime` restores
   timestamp-only behaviour; reach for it deliberately, not to make a rerun go
   away.
2. **Loose wildcards make rules ambiguous.** `{sample}.csv` also matches
   `summary.csv` sitting beside the per-sample files, and Snakemake reports an
   `AmbiguousRuleException`. Pin every wildcard in `wildcard_constraints` to the
   exact values your config or sample sheet yields, rather than to a pattern
   like `[^/]+`.
3. **A rule's outputs must be a function of its wildcards alone.** Two jobs
   writing the same file, or a rule whose output path doesn't carry every
   wildcard it fans out over, produces races that only appear under `-j`.
4. **Directory outputs and temporary files need declaring.** `directory()`,
   `temp()`, `protected()`, `ancient()` change how Snakemake reasons about a
   file's existence and age. An undeclared directory output is the classic cause
   of a rule that never reruns.
5. **Re-running a finished pipeline under a variant policy breaks on the paths
   you did not parameterise.** An *output* still bound to the shared path
   overwrites the finished run's file and then fails the rule; an *input* still
   bound to it reads the finished run's data and succeeds, so the variant's
   results are computed from the wrong inputs with no error anywhere. A dry run
   cannot catch either — it never imports the scripts.
6. **A stage seeded from `hash()` of a string is not a function of its inputs.**
   Python salts str hashing per process, so `abs(hash(key)) % 2**31` as an RNG
   seed reseeds the stream on every run. Snakemake cannot see this: the dry run
   says `Nothing to be done` and the output still differs. Grep the whole tree,
   and measure the run-to-run overlap before interpreting any parameter sweep —
   an unseeded stage becomes the sweep's noise floor.

Two more bite on the first run rather than the hundredth, and both are written
up in the references. A `script:` file must not begin with `from __future__
import ...`: Snakemake prepends its own preamble to a copy of the script, so the
import is no longer first and every job of that rule dies with a `SyntaxError`
naming a temporary file, while the rule's `log:` stays empty. And `--sdm conda`
cannot build an environment when the conda *installation* directory is
read-only — set `CONDA_PKGS_DIRS` to somewhere writable and
`CONDA_REGISTER_ENVS=false`, or watch a fully built env roll itself back.

## Declaring compute

Declare `threads` and `resources` on any rule that isn't trivial, even if you
only ever run locally — it's what lets `-j` schedule sensibly, and it's the
whole interface to a scheduler later.

```python
rule align:
    threads: 8
    resources:
        mem_mb=16000,
        runtime=120,          # minutes; the conventional name schedulers map
    shell:
        "bwa mem -t {threads} ..."   # pass it through, or the reservation lies
```

The **`{threads}`-must-reach-the-command** point is the one people miss: a rule
that reserves 8 and runs a single-threaded command wastes seven cores.

Two profile kinds, and the distinction is load-bearing: `--workflow-profile` is
for settings specific to *this workflow* (resources per rule), while `--profile`
is for settings specific to *this compute environment* (which executor, how many
jobs). A workflow ships the first and the site supplies the second. Snakemake
picks up `profiles/default/config.yaml` automatically — pass
`--workflow-profile none` to bypass it when debugging.

For cluster and cloud execution, v8+ uses executor plugins (`--executor slurm`,
installed as `snakemake-executor-plugin-slurm`); the builtin choices are only
`local`, `dryrun` and `touch`. See `references/execution.md` for the plugin
model, `group:`/`--group-components` batching, and retry handling.

**When a workflow actually goes to a shared cluster, load the
`cannon-allocation-efficiency` skill.** It covers what this one deliberately
does not: whether jobs saturate the cores they reserve, batch granularity
against heavy-tailed per-unit cost, and reserved-versus-used auditing from
`sacct` and `benchmark` records.

## Composing and reusing workflows

Before writing a rule that wraps a common tool, check the [Snakemake Wrappers
repository](https://snakemake-wrappers.readthedocs.io/) — `wrapper:` instead of
`shell:` gets you a tested invocation and a pinned conda env.

To reuse a whole published workflow rather than copying its rules, use the
module system:

```python
module rnaseq:
    snakefile: github("snakemake-workflows/rna-seq-star-deseq2", path="workflow/Snakefile", tag="v2.0.0")
    config: config["rnaseq"]

use rule * from rnaseq as rnaseq_*
```

For a parameter sweep, `snakemake.utils.Paramspace` turns a dataframe of
parameter combinations into wildcard patterns, so the sweep lives in a file
rather than in nested `expand()` calls.

## Reference files

- `references/toolchain.md` — every tool in the gate: exact flags, what each
  produces, the gotchas (report needs `pygments` and a zip for large output
  sets; `--filegraph` is unreadable at whole-workflow scale; unit tests need
  small inputs), plus the inspection and surgical-rerun flags.
- `references/correctness.md` — rerun triggers and corrections that fail to
  propagate, wildcard ambiguity and `ruleorder`, file-modifier semantics,
  input functions and checkpoints, and two cluster failures whose reported
  error points away from the cause.
- `references/execution.md` — local scheduling, executor plugins, profiles,
  job grouping, retries, and conda/apptainer deployment.
