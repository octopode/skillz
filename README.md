# skillz

Agent skills for computational research workflows. Each folder is one
self-contained skill: a `SKILL.md` with YAML frontmatter (`name`,
`description`) plus any supporting files.

| Skill | What it covers |
|---|---|
| [`snakemake-workflow`](snakemake-workflow/) | Building, extending and reviewing Snakemake workflows to upstream best practice: the official layout, the `--lint` / `snakefmt` / `--report` / `--containerize` / `--generate-unit-tests` toolchain, DAG and wildcard semantics that cause real bugs, and threads/resources for local and cluster execution. References are in `references/`. |
| [`cannon-allocation-efficiency`](cannon-allocation-efficiency/) | Checking that cluster jobs can actually saturate the CPU cores and memory they reserve, both before submitting and after running: `{threads}` pass-through, worker pools, nested-parallelism oversubscription, batch granularity, reserved-vs-used audits from `sacct` and Snakemake benchmark records, and SLURM job arrays (limits, `%N` throttles, per-task accounting, and why Snakemake's `--slurm-array-jobs` is unsafe in executor plugin 2.8.0; `array_probe/` holds the test workflow). Written against Harvard FASRC's Cannon cluster, but applies to any SLURM site. |

`snakemake-workflow` hands cluster-sizing questions off to
`cannon-allocation-efficiency`, so install both together.

## Installing

Copy or upload a skill folder into your agent's skills location. For
example, use the Skills settings page in Claude apps, or the skills
directory of whatever agent harness you use. Installed copies are
snapshots. They do **not** update when this repo changes, so pull and
reinstall to get fixes.

`cannon-allocation-efficiency/kernel.py` holds the helper functions that the
skill's examples call (`check_allocations`, `audit_benchmarks`,
`pool_slice_size`, and others). Harnesses that support skill sidecars load it
automatically. Anywhere else, run `from kernel import *` from the skill folder
(or copy the file next to your workflow) before following the examples.

## License

MIT. See [LICENSE](LICENSE).
