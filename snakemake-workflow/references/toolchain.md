# The Snakemake toolchain

Verified against Snakemake 9.27. Each section says what the tool produces, what
it catches, and what bites.

## Contents

- [Linting and formatting](#linting-and-formatting)
- [Reports](#reports)
- [Unit tests](#unit-tests)
- [Containerization](#containerization)
- [Graphs](#graphs)
- [Inspecting what exists](#inspecting-what-exists)
- [Surgical reruns](#surgical-reruns)
- [Publishing a workflow](#publishing-a-workflow)

## Linting and formatting

```bash
snakemake --lint              # plain text
snakemake --lint json         # machine-readable, for a commit hook or CI
```

Upstream describes it as highlighting issues to fix for best practices,
readability and reproducibility, and highly recommends running it before
publishing a workflow. It is explicitly a work in progress, so a clean lint is a
floor, not a ceiling.

Rule-level lints worth acting on immediately: a rule with no `conda:`/`container:`
(the run isn't reproducible), no `log:` (a failure in a large run is
unfindable), an absolute path (the workflow won't move), or a value hardcoded in
`shell:` that belongs in `params:`.

`snakefmt` is the official formatter (`pip install snakefmt`). It is black for
Snakefiles and upstream recommends applying it before publishing:

```bash
snakefmt --check --diff workflow/    # report only
snakefmt workflow/                   # rewrite
```

Two things to check after the first run over an existing workflow. It **reorders
rule sections** into its canonical order (`params:` lands after `conda:`, not
next to `input:`), which is cosmetic but makes the first diff larger than you
expect — take it as its own commit so it doesn't bury a substantive change.
And verify it is **idempotent** on the result: `snakefmt` 2.0.3 has been seen to
relocate a rule's docstring from the top of the rule to below its `log:` and
then fail to reparse its own output (`SyntaxError: Unrecognised keyword` naming
the docstring). Snakemake parses such a file fine, so only `--check` reveals it.
Putting the docstring back at the top of the rule clears it and the formatter
then leaves the file alone. A formatter whose `--check` can never pass is worth
nothing as a gate, so confirm `snakefmt --check` is clean *and* that a second
`snakefmt` pass changes nothing.

Note that `--lint` needs the DAG to build, so it fails on a workflow with a
syntax error or a missing config — fix those with `-n` first.

## Reports

```bash
snakemake --report report.html       # self-contained single file
snakemake --report report.zip        # for anything with many or large results
```

Produces default statistics, provenance information and user-specified results.
Runtime statistics come from the metadata Snakemake stores in the `.snakemake`
directory of the working directory, so **the report describes the last run** —
generate it after a successful build, not before.

Three things to know:

- **It needs `pygments`** alongside the Snakemake driver, or it fails with
  `WorkflowError: Python package pygments must be installed to create reports`.
  Put it in whatever env you run Snakemake from.
- **The results section is empty until you mark outputs.** Wrap the ones a reader
  should see in `report(...)` with a caption:

  ```python
  rule plot:
      output:
          report("results/figures/overview.pdf",
                 caption="../report/overview.rst",
                 category="Overview")
  ```

  The caption is a small reStructuredText file in `workflow/report/`; it can
  interpolate `{{snakemake.wildcards}}` so one caption serves a fanned-out rule.
  Without `report(...)` you still get provenance, per-rule source code, runtime
  statistics, config and package versions — useful, but not a results document.
- **HTML embeds everything.** Upstream notes the single-file form suits only
  smaller reports and recommends a ZIP archive for anything more complex. A
  workflow with dozens of PDFs wants `report.zip`.

`--report-stylesheet` takes a CSS file for house styling. `--reporter PLUGIN`
swaps the builtin HTML reporter for a custom one (plugins take their own
`--report-*` options).

For tabular results, [datavzrd](https://datavzrd.github.io/) renders CSV/TSV
into an interactive HTML report and integrates with Snakemake reporting — worth
it when the deliverable is a table someone needs to sort and filter rather than
a figure.

## Unit tests

```bash
snakemake --generate-unit-tests .tests/unit
pytest .tests/unit
```

It creates one test case per rule: it copies that job's inputs and expected
outputs aside, and on re-execution compares results against those obtained at
generation time. This is the strongest cheap guarantee that a refactor didn't
change numbers.

Two constraints from upstream, both easy to hit:

- **It assumes all input files of each job are already present.** Jobs without
  present inputs are skipped with a warning, so generate tests from a completed
  run.
- **Don't generate from big data.** Upstream is explicit that unit tests should
  finish in a few seconds. If the real inputs are large, make a downsampled test
  dataset first — that work is the prerequisite, not an optional extra.

Generated tests compare outputs byte-for-byte by default, which fails on any
output embedding a timestamp or an unordered dict. The generated
`tests/common.py` has an `OutputChecker` class to subclass for those cases.

## Containerization

```bash
snakemake --containerize > Dockerfile
snakemake --containerize apptainer > workflow.def
```

Prints a container definition providing an execution environment for the
workflow including all conda environments. Formats are `dockerfile` (default)
and `apptainer`. This is the strongest form of "rerunnable from scratch": the
image pins the conda envs your rules declare, so a future run doesn't depend on
conda-forge still serving those builds.

Related: `--software-deployment-method` (aliases `--deployment-method`,
`--deployment`, `--sdm`) takes `conda`, `apptainer`, `env-modules` — this is the
v8+ generalization of `--use-conda`, which still works. `--sdm conda apptainer`
runs conda envs inside a container. `--conda-create-envs-only` pre-builds every
env without running the workflow, which is the right first step on a machine
with slow or rate-limited network.

## Graphs

```bash
snakemake --rulegraph  | dot -Tpdf -o rulegraph.pdf   # one node per rule
snakemake --filegraph  | dot -Tpdf -o filegraph.pdf   # rules + their files
snakemake --dag        | dot -Tpdf -o dag.pdf         # one node per JOB
```

Pick by what you're trying to see, and mind the scale:

- `--rulegraph` is the one for documentation. It stays readable as the workflow
  grows, because fan-out over wildcards doesn't multiply nodes.
- `--filegraph` sits between the two: the dependency graph of rules with their
  input and output files. Upstream recommends it when the job DAG is too large.
  Be warned it does not stay readable on a whole workflow — on a 30-rule
  pipeline it renders several thousand pixels square. Use it on a target
  (`snakemake --filegraph results/thing.csv`), not on `all`.
- `--dag` shows every job. Honest but unreadable past a few dozen jobs.

A `print()` in your Snakefile lands in the dot output and breaks rendering —
that's the usual cause of a graphviz syntax error. Pipe through
`grep -v '^Building DAG'` if the header leaks in.

Generating a graph as a rule means the rule invokes Snakemake recursively, which
means it cannot be part of an ordinary build (the workflow would be scheduling
itself). Either run it by hand after changing the pipeline, or make it an
explicitly-requested target that is not in `rule all`.

## Inspecting what exists

```bash
snakemake --summary            # filename, mtime, rule, status, plan
snakemake --detailed-summary   # adds input files and the shell command
snakemake --list-changes code  # files whose rule's code changed since creation
```

`--detailed-summary` gives filename, modification time, rule version, input
files, shell command, status and plan, where status says whether the file is
missing, its inputs are newer, or the rule's version or implementation changed
since the file was created. This is the file-level inventory of a workflow, and
it's the fastest way to answer "why does it want to redo that".

`--list-changes {code,input,params}` narrows it to one reason.

## Surgical reruns

When you need to rebuild part of a workflow without redoing all of it:

| flag | effect |
|---|---|
| `--forcerun RULE_OR_FILE` | rerun this and everything downstream |
| `--until RULE_OR_FILE` | stop once this is done |
| `--omit-from RULE_OR_FILE` | build everything except this and its descendants |
| `--batch RULE=i/n` | split a fan-out rule's jobs into n batches, run the i-th |
| `--touch` | mark outputs up to date without running (use with care) |
| `--all-temp` | treat every output as `temp()`, for a disk-constrained rerun |
| `--unlock` | release a stale lock after a killed run |
| `--cleanup-metadata FILE` | forget a file's provenance so it stops triggering reruns |

`--touch` and `--cleanup-metadata` both make Snakemake believe something it
hasn't verified. They are the right tool for recovering from an interrupted run
or a metadata mishap, and the wrong tool for making an inconvenient rerun go
away — that rerun is usually telling you the truth.

## Publishing a workflow

Sticking to the standardized folder structure simplifies navigating the codebase
for frequent users, and it is what the [Snakemake Workflow
Catalog](https://snakemake.github.io/snakemake-workflow-catalog/) needs: with a
`config/README.md` and standardized usage declared in the repo, the catalog
generates usage documentation automatically. `snakedeploy` then lets someone
deploy your workflow into their own project without copying it.
