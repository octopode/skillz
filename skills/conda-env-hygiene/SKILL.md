---
name: conda-env-hygiene
description: "Keep Claude Science conda environments lean and conflict-free: run in any env that already has everything, but never add packages to the shared starter python/r envs or to another project's env; missing packages go into one named env per project or tool stack, created in one go with a pinned Python, conda-forge/bioconda before pip, with an environment.yml written into the project folder. Use this skill BEFORE any package install or environment choice - whenever a task needs a package the starter python/r envs lack, an ImportError or ModuleNotFoundError appears, manage_environments or manage_packages is about to be called, someone wants pip install, or an environment.yml must be exported, rebuilt or audited. Ships stdlib-only helpers (kernel.py) that find which envs already hold a package, check conda-forge/bioconda for a build, export a from-history environment.yml including pip requests, and audit envs for drift."
---

# conda-env-hygiene

Conda's own guidance, applied to Claude Science: isolate each project in its own
environment, install everything at once, use conda before pip and pip only last,
rebuild rather than run conda after pip, and keep the requirements in a file
([conda docs: managing environments](https://docs.conda.io/projects/conda/en/latest/user-guide/tasks/manage-environments.html),
[Anaconda: using pip in a conda environment](https://www.anaconda.com/blog/using-pip-in-a-conda-environment)).
Two purposes: (1) keep the shared starter envs (`python`, `r`) lean, because every
package added to them slows every session that loads them; (2) avoid version
conflicts between projects. Reading from any env is harmless; adding to the wrong env
is the problem. This skill overrides the generic "reuse the env with most of the
packages and add the rest" habit: running in any env that already has everything is
fine, but missing packages go only into an env this project owns, otherwise into a
new one.

## How environments work here (read once)

- Claude Science runs its own micromamba (`~/.claude-science/conda`, with `--no-rc`,
  so the user's `.condarc` and personal conda are untouched). Envs live in
  `~/.claude-science/conda/envs/`, **read-only from the sandbox**.
- Only `manage_environments` and `manage_packages` change an env. There is no
  `conda`/`micromamba` on PATH; `conda activate`, `conda run -n`, `micromamba run`
  do not apply. Select an env by passing `environment="<name>"` on every
  `python`/`bash`/`r` call.
- A shell `pip install` (bash, `%pip`, `subprocess`) lands in the per-session
  overlay `./.venv/<env>` and disappears with the workspace. Never use it as an
  install method.
- Envs are shared by all projects on the machine, so names are global.
- Creating an env shows the user one permission card listing its name and packages;
  later installs show more. Batch everything into the creation call.
- `manage_packages(mode="install")` runs with `--freeze-installed`: it will not
  upgrade what is there. A solve failure on install is a signal to rebuild.

## Helpers (kernel.py, loaded with this skill, stdlib only)

| Helper | Purpose |
|---|---|
| `conda_find(pkgs)` | which envs already have each package (version, conda or pip) |
| `conda_channel_lookup(names)` | is there a conda-forge/bioconda build for this platform? |
| `conda_env_spec(env)` | explicit requests: channels, conda specs, pip requests, transaction count |
| `conda_env_yml(env, path)` | write a from-history `environment.yml` (pip section included) |
| `conda_yml_to_create(path)` | turn an `environment.yml` into `manage_environments` arguments |
| `conda_env_audit(env, check_channels=False)` | drift and conda/pip-order findings |
| `conda_env_names()`, `conda_installed(env)`, `conda_env_subdir(env)` | building blocks |

They read `conda-meta/history` (the `# update specs` / `# remove specs` lines, i.e.
what `conda env export --from-history` reads) and pip `*.dist-info` folders whose
`INSTALLER` is `pip` and that hold a `REQUESTED` marker (explicit pip installs, not
their dependencies). The envs root is `dirname(sys.base_prefix)`; override with
`root=` or `$CONDA_ENVS_ROOT`. `conda_channel_lookup` downloads
`https://conda.anaconda.org/<channel>/channeldata.json` (about 24 MB conda-forge,
12 MB bioconda; cached 24 h in `./.conda-channeldata/`), because `api.anaconda.org`
is denylisted in the sandbox.

## Workflow: a task needs packages

1. **Write down the complete list** the task will need, including what loaded
   skills' `kernel.py` files import (see pitfalls) and `pip` if any PyPI-only package
   is expected. Decide the project: the granted folder or repo being worked on, or,
   if there is none, the tool stack (e.g. `geospatial`, `browser`).
2. **Starter envs (`python`, `r`)**: never install into, rebuild or delete them,
   even when the audit flags drift: they are the app's, shared by every project, and
   past work relies on what they hold. Running in them is fine when they already have
   everything.
3. **Does some env already have everything?** Run `conda_find([...])` (or
   `manage_environments(mode="list", dependencies=[...])`). If one env has usable
   versions of all the packages, run the work there as it is, with no installs and no
   export; say which env was used. Any env qualifies, including another project's.
4. **Something is missing.** Install only into an env this project or stack owns:
   one named after it, or named by memory or by an `environment.yml` in the project
   folder. Never add packages to a starter env or to an env that belongs to another
   project, even if it lacks only one package: that is how cross-project version
   conflicts start. If this project owns no env, create one (step 6) with the full
   list, including what another env already had.
5. **Conda first.** `conda_channel_lookup([...])` for anything not obviously on
   conda-forge. PyPI and conda names usually match after normalisation, but not
   always (`torch` = `pytorch`, `tables` = `pytables`, `opencv-python` = `opencv`);
   check the conda-forge name before falling back to pip. Bio tools: add `bioconda`
   after `conda-forge`. Exception in `PIP_PREFERRED`: `playwright` (conda-forge has
   only the CLI).
6. **Create once, with everything**:
   ```
   manage_environments(mode="create", name="<project-or-stack>",
       python_version="3.12", channels=["conda-forge"],   # + "bioconda" if needed
       packages=["xarray", "netcdf4", "pandas", "pip", ...])
   ```
   Pin Python to a minor version; constrain other packages only where a
   version actually matters. Names: lowercase, hyphenated, descriptive; check
   `conda_env_names()` that the name is free. For R:
   `language="r"`, packages as `r-<name>` from conda-forge, Bioconductor as
   `bioconductor-<name>` from bioconda.
7. **PyPI-only packages: immediately after creation**, all in one call:
   `manage_packages(mode="install", environment=..., packages=[...], use_pip=True)`.
8. **Export** (envs this project owns): `conda_env_yml("<env>", "<project folder>")` writes
   `<project folder>/environment.yml`. With no writable project folder, write it to
   the workspace and `save_artifacts` it. Re-export after every change. Default
   `versions="comment"` records the installed versions as comments; `versions="pin"`
   pins them for an exact same-platform rebuild.
9. Run the work with `environment="<env>"`.

## Adding to an env this project owns

- Env has **no pip requests** (`conda_env_spec(env)["pip"] == []`): one
  `manage_packages` install with all the new conda packages is acceptable. Re-export.
- Env **has pip requests** and the new package is conda: do not run conda after pip.
  Rebuild: `spec = conda_env_spec(env)`, add the new packages, then ask the user
  (`ask_user`) whether to delete and recreate under the same name or create
  `<env>-2`; create with the merged conda list, then the pip list, then re-export.
- New package is PyPI-only: a further pip install is fine (pip after pip).
- A `--freeze-installed` solve failure: rebuild the same way, from the merged spec.

## Rebuilding from an environment.yml

```python
args = conda_yml_to_create("<project folder>")   # or the file path
# -> {'name', 'channels', 'python_version', 'packages', 'pip'}
```
Then `manage_environments(mode="create", name=args["name"],
python_version=args["python_version"], channels=args["channels"],
packages=args["packages"])` and, if `args["pip"]`,
`manage_packages(..., use_pip=True)`. Outside Claude Science the same file works
with `conda env create -f environment.yml`.

## Auditing

```python
for e in conda_env_names():
    for level, msg in conda_env_audit(e, check_channels=True):
        print(e, level, msg)
```
Findings: starter env changed since creation; installs after creation (rebuild to
let the solver see everything at once); conda ran after pip; pip overwrote a conda
package's Python files; pip-installed packages that have a conda build; unpinned
Python. Report findings; do not fix them unasked. Starter-env findings are
informational only (see workflow step 2).

## Deleting

Delete an env only after the user agrees in `ask_user` (name it, say what uses it,
and that its `environment.yml` exists). Export first.

## Pitfalls seen on this install

- **`referencing` ships a file named `.git`.** conda-forge `referencing` (pulled in by
  `jsonschema`, so by snakemake, jupyter, nbformat) contains
  `etc/conda/test-files/referencing/1/suite/.git`. While the sandbox runs git
  protection in COARSE mode (stderr banner "git protection is running in COARSE
  mode"), that file cannot be written and env creation rolls back. Workaround: create
  the env without the jsonschema-dependent packages, then pip-install
  `referencing jsonschema snakemake` (PyPI wheels carry no `.git`). This is the one
  accepted exception to conda-first; note it in the `environment.yml` header.
  Narrowing the folder grants can restore fine-grained git protection.
- **`snakemake --use-conda` inside the sandbox** builds envs with its own conda, and
  needs `CONDA_PKGS_DIRS=<writable dir>` (else NoWritablePkgsDirError) plus
  `CONDA_REGISTER_ENVS=false` (else a rollback on `~/.conda/environments.txt`). See the
  `snakemake-workflow` skill.
- **Skill sidecars load into whatever env the kernel runs.** A skill's `kernel.py`
  that imports pandas (or anything else) at top level fails in a lean dedicated env.
  Include such packages in the env's creation list, or call those helpers from an env
  that has them. When writing sidecars, keep third-party imports inside functions.
  This skill's helpers are stdlib-only and load anywhere.
- **pip can hide behind a conda name**: `conda_installed(env)` marks pip records that
  replaced a conda record (`over_conda`); the audit warns only when the conda package
  had Python files.
