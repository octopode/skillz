# skillz

Agent skills for computational research workflows. Each skill is one folder
under [`skills/`](skills/): a `SKILL.md` with YAML frontmatter (`name`,
`description`) plus any supporting files.

| Skill | What it covers |
|---|---|
| [`snakemake-workflow`](skills/snakemake-workflow/) | Building, extending and reviewing Snakemake workflows to upstream best practice: the official layout, the `--lint` / `snakefmt` / `--report` / `--containerize` / `--generate-unit-tests` toolchain, DAG and wildcard semantics that cause real bugs, conda deployment pitfalls, and threads/resources for local and cluster execution. References are in `references/`. |
| [`cannon-allocation-efficiency`](skills/cannon-allocation-efficiency/) | Checking that cluster jobs can actually saturate the CPU cores and memory they reserve, both before submitting and after running: `{threads}` pass-through, worker pools, nested-parallelism oversubscription, batch granularity, reserved-vs-used audits from `sacct` and Snakemake benchmark records, and SLURM job arrays (limits, `%N` throttles, per-task accounting, and why Snakemake's `--slurm-array-jobs` is unsafe in executor plugin 2.8.0; `array_probe/` holds the test workflow). `guard/` ships an `sbatch` shim that checks every submission. Written against Harvard FASRC's Cannon cluster, but applies to any SLURM site. |
| [`openathens-fetch`](skills/openathens-fetch/) | Downloading paywalled journal PDFs through your own library OpenAthens login (Claude Science on macOS). A Chrome login window, run outside the sandbox, captures only the identity cookies (OpenAthens plus your institution's login site); the agent replays them through the publisher SAML chain, re-authenticates silently while your institution session lives, and opens the login window again (optionally by itself, via a LaunchAgent) when it does not. Defaults to Harvard Library; any OpenAthens institution works. Setup is in the skill's `README.md`. |
| [`conda-env-hygiene`](skills/conda-env-hygiene/) | Keeping Claude Science conda environments lean and conflict-free: work runs in any env that already has everything, but packages are never added to the shared starter `python`/`r` envs or to another project's env. Missing packages go into one named env per project or tool stack, created in one go with a pinned Python; conda-forge/bioconda before pip, and pip only right after creation; an `environment.yml` written into the project folder. Stdlib-only helpers in `kernel.py` find which envs already hold a package, check conda-forge/bioconda for a build on your platform, export a `--from-history`-style `environment.yml` that also keeps explicit pip installs, and audit envs for drift. Claude Science-only. |

`snakemake-workflow` hands cluster-sizing questions off to
`cannon-allocation-efficiency`, so install both together.

## Installing

**Claude Science:** Settings > Skills > Add skill > **Import from GitHub**, and
paste `https://github.com/octopode/skillz`. All skills are imported from the
default branch, and Claude Science records the commit they came from. Imported
skills do not update on their own: use **Check for updates…** on the repo's entry
under Settings > Skills > Imported to pull newer commits.

**Claude Code** (`openathens-fetch` and `conda-env-hygiene` are Claude Science-only and are not in the plugin):

```
/plugin marketplace add octopode/skillz
/plugin install skillz@skillz
```

**Anything else:** copy a folder from `skills/` into your agent's skills
directory. Copies are snapshots, so pull and recopy to get fixes.

`skills/cannon-allocation-efficiency/kernel.py` holds the helper functions that
the skill's examples call (`check_allocations`, `audit_benchmarks`,
`pool_slice_size`, and others). Harnesses that support skill sidecars load it
automatically. Anywhere else, run `from kernel import *` from the skill folder
(or copy the file next to your workflow) before following the examples.

The `guard/` scripts run on the cluster, not in the agent. Copy them to the
cluster, then `chmod +x` them, since an import may not keep the executable bit.
Put the folder ahead of the real `sbatch` on `PATH`, as the skill describes.

`openathens-fetch` copies its `launcher/` into a session folder with `oa_install()`,
which sets the executable bits itself; see `skills/openathens-fetch/README.md`.

## Cluster notes

[`cluster-notes/cannon.md`](cluster-notes/cannon.md) is a working field guide to Harvard FASRC's
Cannon cluster: partitions, maintenance reservations, scheduler and storage quirks, and
job-array limits, as found in day-to-day use. It is mirrored automatically, with personal
identifiers replaced, every time the maintainer's notes change, so do not edit it here. Open an
issue with corrections. Some passages refer to the maintainer's own projects and agent harness;
the cluster facts stand on their own.

## License

MIT. See [LICENSE](LICENSE).
