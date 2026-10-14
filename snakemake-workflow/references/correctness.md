# Snakemake semantics that cause real bugs

Verified against Snakemake 9.27. These are the behaviours that differ from what
a `make` user expects, or that fail only under parallelism.

## Contents

- [Why a job reruns](#why-a-job-reruns)
- [A correction that does not propagate](#a-correction-that-does-not-propagate)
- [When a rule fails and the log does not say why](#when-a-rule-fails-and-the-log-does-not-say-why)
- [Parameterising a finished pipeline into variants](#parameterising-a-finished-pipeline-into-variants)
- [An input Snakemake cannot see](#an-input-snakemake-cannot-see)
- [A rule that is not a function of its inputs](#a-rule-that-is-not-a-function-of-its-inputs)
- [A force that forces nothing](#a-force-that-forces-nothing)
- [Wildcard ambiguity](#wildcard-ambiguity)
- [Outputs must be a function of wildcards](#outputs-must-be-a-function-of-wildcards)
- [File modifiers](#file-modifiers)
- [Input functions](#input-functions)
- [Checkpoints, for when the DAG is not known upfront](#checkpoints-for-when-the-dag-is-not-known-upfront)
- [Scripts and the snakemake object](#scripts-and-the-snakemake-object)
- [Config that cannot silently drift](#config-that-cannot-silently-drift)

## Why a job reruns

The default is `--rerun-triggers code input mtime params software-env`, and
upstream's own framing is that using all of them guarantees results are
consistent with the workflow code and configuration.

So: **editing a script reruns every job that uses it, and changing a `params`
value reruns the jobs it reaches.** That is the correct behaviour for an
analysis pipeline, and it is the single most surprising thing about Snakemake if
you arrive from `make`, where only timestamps matter.

`--rerun-triggers mtime` restores timestamp-only behaviour. Use it when you
know the change was cosmetic (a comment, a rename) and the rerun would cost
hours. Don't make it a habit — a stale result that matches no version of the
code is worse than a slow build.

To find out *which* trigger fired, `snakemake --list-changes code` (or `input`,
`params`) names the files whose rule changed since they were created;
`--detailed-summary` gives the same per-file with a status column.

**A file declared as an input but read via a path string won't trigger
anything.** If a script opens a path Snakemake doesn't know about, no rerun
happens when it changes — and no error either. This is the failure mode behind
"why didn't it notice". If you deliberately want a file *not* to invalidate
computation (a frequently-edited metadata sheet, say), pass its path through
`params:` rather than `input:` and accept that changes then need `--forcerun`.
Make that choice explicitly and write down why, because the next reader will
assume it was an oversight.

## A correction that does not propagate

Snakemake will not rebuild a missing intermediate whose downstream descendant
already exists. That is usually what you want — it is why deleting a scratch
file does not cost you an hour of recomputation. It becomes a correctness
problem the moment you fix an input and re-run.

The shape: you correct something upstream, re-run, and the rebuild propagates a
few stages and then stops. Snakemake prints

    Nothing to be done (all requested files are present and up to date).

and exits 0. Every stage below the stopping point is still the pre-correction
artefact, and the workflow is telling you it is finished.

A real instance: an audit changed a sample-exclusion set, the rebuild reached
the per-family FASTAs, and everything below was pruned because the next stage's
aggregate flag from eleven days earlier still existed and its own consumers
looked satisfied. The 66 missing per-batch flags were never queued. The driver
exited clean in 99 seconds.

**`rc=0` is not evidence that a rerun happened.** Two checks, neither expensive:

- Walk the mtimes down the dependency chain. Any output that predates its own
  inputs while the workflow claims completion is a pruned branch.
- Compare `--dry-run` against `--dry-run --forcerun <first affected rule>`. In
  that instance the baseline listed no jobs and the forced run listed ~600.

The fix is not to remember to pass `--forcerun`. Put the check in whatever
launches the workflow: compare the two boundary files and add the force itself.
Make it self-limiting — once the branch rebuilds, the downstream file is newer
and the condition stops firing, so a resubmit does not redo the work.

Note the second layer, which `--forcerun` alone does not solve. If the rule's
script decides per-unit whether its output is current, forcing the rule reruns
the script and the script still skips everything: it has no way to know that
the *inputs to that decision* changed. In the same instance the forced rule
reported "building 0". The script needs its own force switch threaded through
`params:`, and you need both.


## When a rule fails and the log does not say why

Two cluster failure modes where the reported error points away from the cause.

**A rule that was never submitted.** If a rule fails instantly and repeatedly
and leaves no `.snakemake/slurm_logs/rule_<name>/*.log` and no `sacct` row, the
job was never created — the scheduler rejected the submission. Snakemake can
surface this as nothing more than `(command exited with non-zero exit code)`.
Grep the driver log for `sbatch: error`. One real example: a site forbids a
particular partition in a multi-partition list, which made every rule carrying
that resource permanently unsubmittable while the error text talked about exit
codes. The absence of both a rule log and a scheduler record is the signature.

**A `touch()` output that does not land.** `output: touch("flags/{i}.done")`
leaves flag creation to Snakemake after the shell returns. Observed under a
remote executor: the job exits 0, the flag's parent directory is present and
empty, the flag is still missing after `--latency-wait 300`, and the rule fails
with `MissingOutputException`.

Before believing any of the obvious explanations, test it. All of these were
plausible and all were wrong in that case:

| suspected cause | the test that killed it |
| --- | --- |
| filesystem write latency | 40 files written from 40 tasks across 8 nodes were 40/40 visible at t=0, on both Lustre and NFS |
| concurrent drivers racing jobs | reproduced with a single driver and no restarts |
| conda-wrapper lock contention | the lock warning appeared in 40/40 invocations and caused 0/40 failures |
| the storage layer | a toy workflow carrying the same `--shared-fs-usage` and `--local-storage-prefix` flags touched correctly |
| a concurrency race | reproduced with one job and nothing else running |

That last row is the one worth reaching for early: if a failure reproduces at
concurrency 1, every race explanation is dead at once.

The mechanism there was never identified. What removes the exposure is not
depending on the post-job action — have the rule create its own flag:

    shell:
        '... && mkdir -p "$(dirname {output})" && touch "{output}"'

Leaving `touch()` on the output as well is harmless; it is a no-op once the file
exists. This is a workaround rather than a diagnosis, and it is worth saying so
in a comment, so the next reader does not assume the cause was understood.


## An input Snakemake cannot see

A rule that reads a database, an API, or any mutable store usually cannot name
that store in `input:` — so pipelines represent it with a sentinel flag
(`data/interim/flags/ingest.done`) produced by whichever rule last wrote it.
That works only while every write refreshes the sentinel. When something writes
the store outside the rule that owns the flag, the DAG goes quiet: no rule's
inputs changed, so an unforced dry run truthfully reports `Nothing to be done`
over a result computed from data that no longer exists.

Nothing in the DAG can catch this, because by construction the dependency is not
in the DAG. Two habits do:

**Order the inputs by mtime before trusting a finished artifact.** The store
being newer than the artifact that read it is the whole signature:

    matrices (newest)   13:06
    matrices.done       13:07
    results.parquet     15:19      <- the artifact
    dataset.duckdb      15:29      <- an input, written AFTER it

**Make comparability a computed check, not an assumption.** When several runs of
the same pipeline are compared, find a quantity the varied parameter *cannot*
change — a sample size, a row count, a checksum of the input slice — and assert
it agrees across runs. A pair that disagrees on it was fitted on different
inputs, and any difference between them is measuring that, not the parameter.
Report the check next to the results so a reader can see which comparisons are
load-bearing.

### Read-only connections and pending writes

One specific trap, because the fix and the bug are the same change. Concurrent
batch jobs that open a DuckDB read-write will fail on lock contention
(`Could not set lock ... Conflicting lock is held in PID N`), and the natural fix
is to open pure readers read-only. But a read-only connection cannot replay or
claim a pending write-ahead log. If a WAL is outstanding, those readers see the
pre-WAL snapshot — silently, with no error — until some later read-write open
checkpoints it.

So the lock fix can convert a loud failure into a quiet one: the jobs now
succeed, and they succeed on stale data. If you make readers read-only,
checkpoint the store first and treat "no writer is running" as a precondition of
the batch stage rather than a coincidence.

## A rule that is not a function of its inputs

Snakemake's whole model assumes a rule is a pure function of its inputs: same inputs,
same outputs. A stage seeded from a non-deterministic source breaks that assumption, and
nothing in Snakemake can detect it. The dry run says "Nothing to be done", the DAG is
clean, the checksums of the *inputs* match — and the output differs from last time.

The common instance in Python:

```python
rng = np.random.default_rng(np.random.SeedSequence([seed, abs(hash(key)) % 2**31, pos]))
```

`hash()` on a `str`, `bytes` or `datetime` is salted per interpreter unless
`PYTHONHASHSEED` is pinned, so this stream is reseeded on every run. The code looks
seeded — there is a literal `seed` right there in the call — which is what lets it
survive review. Use a digest instead:

```python
def stable_seed(*parts: object) -> int:
    key = "\x00".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=4).digest(), "big")
```

Two things make this worth hunting for deliberately rather than waiting to notice it:

**It is usually in more places than the one you found.** Grep the whole tree
(`grep -rn 'hash(' src/`), not the module you were reading. In one pipeline this had
already been found and fixed twice, each time with a comment explaining the bug — and
three further call sites were still live, one of them in a downstream stage whose
published statistics also moved run to run.

**It silently becomes the noise floor of every comparison you run.** If you sweep a
parameter and compare which features each setting selects, an unseeded stage means part
of the difference you measure is the RNG. Measure the floor before interpreting the
sweep: re-run one condition twice on identical inputs and take the overlap. In the case
above, re-running the same policy moved 9% of the called set, so a reported Jaccard of
0.61 between two policies had to be read against a ceiling of 0.91, not 1.0 — and any
pair separated by less than that was not interpretable at all.

Pinning `PYTHONHASHSEED` in the environment is not the fix. It makes the current machine
reproducible while leaving the artifact's provenance dependent on an environment
variable no one will remember to set, and it does nothing for the other
non-deterministic seeds (wall-clock, `os.urandom`, iteration order of a set).

## Parameterising a finished pipeline into variants

A pipeline that has already produced a result often has to be re-run under a
variant policy — a different covariate, reference, threshold or filter — with
the finished run kept intact alongside it. The usual shape is a resolver that
returns a per-variant output directory, with products computed *before* the
parameter enters left on the shared path so they are not rebuilt per variant:

```python
def interim() -> Path:                 # variant-dependent products
    t = os.environ.get("PIPE_VARIANT", "base")
    base = ROOT / "data" / "interim"
    return base if t == "base" else base / "var" / t

def shared_interim() -> Path:          # built before the parameter applies
    return ROOT / "data" / "interim"
```

Point the DEFAULT at the finished run's historical paths. If an unset variable
resolves anywhere else, every completed artifact looks missing and the whole
pipeline rebuilds.

**The failure is in the paths you did not convert.** Converting a module's
obvious outputs and leaving its other path constants alone produces two
different bugs, and only one of them is loud:

| binding | under a variant | symptom |
|---|---|---|
| an output still on the shared path | writes the variant's numbers over the finished run's file, then fails because the rule was watching the variant path | `MissingOutputException`, plus a corrupted baseline |
| an **input** still on the shared path | reads the FINISHED run's table and succeeds | none — the variant's results are computed from the wrong inputs |

The second is the dangerous one. A variant whose site-selection step still reads
the baseline's results table produces a complete, plausible, entirely wrong
answer with no error anywhere. It is only ever caught by looking.

**Gate the launch on a resolution check, not on a dry run.** A dry run confirms
the DAG; it does not import the scripts, so it cannot see which path a module
binds at import time. The check that does has to run each module in a fresh
interpreter per variant, because these constants are evaluated once on import:

```python
CHILD = "import importlib,sys; sys.path.insert(0,%r); " \
        "m=importlib.import_module(%r); print(getattr(m,%r))"

env = dict(os.environ, PIPE_VARIANT=variant)
subprocess.run([sys.executable, "-c", CHILD % (root, mod, attr)], env=env, ...)
```

Assert both directions. Variant-dependent paths must land under the variant;
shared products must land on the historical path for *every* variant — a shared
product that follows the variant quietly multiplies the most expensive stage by
the number of variants. Include paths returned by functions, not just
module-level constants: a `def report_path(x)` is exactly where a missed
conversion hides from a constant scan.

Two further habits make the baseline's survival checkable rather than assumed.
Have each variant run assert the baseline is clean *before* it starts and again
when it finishes — an unforced dry run of the baseline returning "Nothing to be
done" both times. And record the baseline's output mtimes in the run's own log,
so "the finished run was not touched" is evidence in the record rather than a
claim. Commit the finished run's reports beforehand; if a variant does overwrite
one, `git checkout --` is then the whole recovery.

A last trap specific to this refactor: a resolver that replaces a module
constant the tests monkeypatch will silently ignore the override, and the tests
will write their fixtures into the live tree while failing for a reason that
looks unrelated. Keep the constant as an honoured override:

```python
root = BATCH_ROOT if BATCH_ROOT is not None else _paths.batch_root(stage)
```

## A force that forces nothing

`--forcerun RULE` is not an instruction to run that rule. It marks jobs **already in the
DAG** as needing to run. If the rule contributes no job to the DAG that was built, the
flag matches nothing, and Snakemake does not warn you — you get a clean exit and
`Nothing to be done (all requested files are present and up to date)`.

Two ways to land there, both common when re-running a finished pipeline:

**The rule's jobs come from a checkpoint.** When `site_batch` is expanded from
`checkpoints.plan_sites.get(...)` via `glob_wildcards`, its jobs exist only after the
checkpoint re-evaluates. Forcing the downstream rule alone leaves the checkpoint
satisfied, so no jobs are generated and nothing is forced. Force the checkpoint's
consumer as well, so the DAG has a reason to be rebuilt:

```bash
snakemake --forcerun site_batch site_merge --until site_merge   # works
snakemake --forcerun site_batch --until site_merge              # silent no-op
```

**`--until` truncated the DAG before the force could apply.** `--until RULE` builds the
DAG for the requested target and cuts it at `RULE`. If `RULE`'s outputs are already
up to date the truncated DAG is empty, and an empty DAG has nothing to force. The same
`--forcerun` against a target with missing downstream outputs behaves as expected, which
is what makes this so easy to misdiagnose: the flag "worked last time".

**Verify the force landed, every time.** The cost of not checking is a run that reports
success while recomputing nothing — and if you then build on its outputs, everything
downstream inherits the state you thought you had replaced. Two cheap checks:

```bash
snakemake -n --forcerun RULE ... | grep -E '^[a-z_]+ +[0-9]+$|^total'   # before
grep -c 'rule RULE' run.log                                             # after: not 0
```

A timestamp is the other tell. If the artifact you meant to regenerate is older than the
commit that changed how it is computed, it was not regenerated, whatever the exit code
said.

## Wildcard ambiguity

Snakemake matches output paths by pattern, so a loose wildcard silently claims
files you didn't mean. The classic:

```python
rule per_sample:
    output: "results/{sample}.csv"

rule summary:
    output: "results/summary.csv"     # also matches {sample}!
```

`{sample}` defaults to matching almost anything, so `results/summary.csv` has
two candidate rules and Snakemake raises `AmbiguousRuleException`. The fix is
to constrain the wildcard to the values it can actually take:

```python
wildcard_constraints:
    sample="|".join(re.escape(s) for s in SAMPLES),
```

Pinning to the **exact value set** your config or sample sheet yields is better
than a pattern like `[^/]+`, because the constraint then documents the domain
and fails loudly when a value goes missing, rather than quietly matching a
neighbour. Constraints can also be declared per-rule, or inline as
`{sample,[A-Z]+}`.

`ruleorder: specific > general` resolves an ambiguity you cannot design away.
Reach for it second — a constraint expresses intent, a ruleorder just picks a
winner.

## Outputs must be a function of wildcards

Every wildcard a rule fans out over has to appear in its output paths. If two
jobs of the same rule can resolve to the same output file, they race, and the
symptom is a truncated or interleaved file that only appears under `-j` and
vanishes on `-j1`. The same applies to a log or benchmark path: give it the
wildcards too, or parallel jobs overwrite each other's logs.

Related: two *different* rules must not write the same file. Snakemake catches
this at DAG build for identical paths, but not when one rule writes into a
directory another rule declares.

## File modifiers

| modifier | meaning | when it matters |
|---|---|---|
| `temp(...)` | delete once all consumers are done | large intermediates; combine with `--all-temp` for a disk-tight rerun |
| `protected(...)` | make read-only after creation | expensive outputs you don't want an accidental rerun to clobber |
| `ancient(...)` | ignore this input's timestamp | a reference file you refresh without wanting to rebuild the world |
| `directory(...)` | output is a directory, not a file | **required** — an undeclared directory output is the usual cause of a rule that never reruns |
| `touch(...)` | create an empty sentinel | a rule whose real effect is elsewhere |
| `ensure(..., non_empty=True)` | fail if the output is empty or fails a checksum | catching a tool that exits 0 having written nothing |

`directory()` deserves emphasis: Snakemake cannot tell whether a directory's
*contents* are complete, so it treats existence as done. If a rule writes an
unpredictable set of files, either declare `directory()` and accept coarse
invalidation, or use a `checkpoint`.

`localrules: all, clean` keeps trivial rules on the submit host instead of
queuing them as cluster jobs.

## Input functions

An input that depends on the wildcards or the config is a function taking
`wildcards`:

```python
def units_for_sample(wildcards):
    return SAMPLES.loc[wildcards.sample, "fastq"].tolist()

rule trim:
    input: units_for_sample
```

Two things to know. The function runs at DAG-build time, so it cannot depend on
a file the workflow hasn't produced yet — that's what checkpoints are for. And
`--lint` will nudge you to keep these helpers in `common.smk` rather than beside
the rules, which keeps the rule files readable as a list of steps.

`unpack()` lets a function return a dict or list that becomes named inputs.

## Checkpoints, for when the DAG is not known upfront

When a rule's output determines *how many* downstream jobs exist — a clustering
step that emits an unknown number of clusters, a filter that decides which
samples survive — declare it a `checkpoint` and have downstream inputs consult
`checkpoints.<name>.get(**wildcards).output`. Snakemake re-evaluates the DAG
after the checkpoint completes.

This is the correct tool where people otherwise reach for a nested Snakemake
call or a `directory()` output plus a glob.

## Scripts and the snakemake object

Prefer `script:` over `shell:` for anything with logic. The script receives a
`snakemake` object carrying `input`, `output`, `params`, `wildcards`, `config`,
`log`, `threads` and `resources`, so paths are never rebuilt by hand and the
script has no CLI to keep in sync. Paths in `script:` are relative to the rule
file, not the working directory.

Keep assertions in the script rather than only in review. A script that checks
its own assumption — the join produced no unexpected nulls, the parser found as
many records as the header claimed — turns a silent wrong answer into a failed
build, which is the whole point of running under a workflow manager. This costs
one line and is the highest-value habit in this document.

For tools with a standard invocation, `wrapper:` pulls a tested rule body and a
pinned env from the Snakemake Wrappers repository instead of you writing either.

## Config that cannot silently drift

```python
from snakemake.utils import validate
configfile: "config/config.yaml"
validate(config, "workflow/schemas/config.schema.yaml")

samples = pd.read_csv(config["samples"], sep="\t").set_index("sample")
validate(samples, "workflow/schemas/samples.schema.yaml")
```

`validate(data, schema, set_default=True)` fills in schema defaults, so the
shipped config can stay short while scripts still see every key. A typo'd key
then fails at parse time with the schema's own message instead of surfacing as a
`KeyError` inside job 47 of 200.

`onstart`, `onsuccess` and `onerror` handlers in the Snakefile are the place for
run-level side effects — writing a resolved-parameters record, sending a
notification — rather than smuggling them into a rule.
