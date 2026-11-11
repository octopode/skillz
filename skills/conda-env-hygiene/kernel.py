"""conda-env-hygiene helpers. Standard library only, so the file loads in any env."""
import datetime
import glob
import gzip
import json
import os
import re
import sys
import time
import urllib.request

STARTER_ENVS = ("python", "r")
HIDDEN_ENVS = ("claude-science-mcp",)
# conda builds that are not the Python package (conda-forge playwright = CLI only); pip is right here.
PIP_PREFERRED = ("playwright",)


def conda_envs_root(root=None):
    """Directory holding the Claude Science conda envs (the running env's parent)."""
    if root:
        return os.path.expanduser(root)
    if os.environ.get("CONDA_ENVS_ROOT"):
        return os.environ["CONDA_ENVS_ROOT"]
    if os.path.isdir(os.path.join(sys.base_prefix, "conda-meta")):
        return os.path.dirname(sys.base_prefix)
    return os.path.expanduser("~/.claude-science/conda/envs")


def conda_norm(name):
    """Normalise a package name so conda and pip spellings compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def conda_spec_name(spec):
    """Package name of a match spec: 'conda-forge::pandas<3' -> 'pandas'."""
    return re.split(r"[\s<>=!~\[]", spec.split("::")[-1].strip(), maxsplit=1)[0]


def conda_env_names(root=None, include_hidden=False):
    """Envs under the root that have a conda-meta/history (app-internal ones hidden)."""
    root = conda_envs_root(root)
    out = []
    for e in sorted(os.listdir(root)):
        if e.startswith(".") or (e in HIDDEN_ENVS and not include_hidden):
            continue
        if os.path.exists(os.path.join(root, e, "conda-meta", "history")):
            out.append(e)
    return out


def conda_installed(env, root=None):
    """Installed packages: {norm_name: {'name','version','by', ...}}; pip wins name clashes."""
    prefix = os.path.join(conda_envs_root(root), env)
    pkgs = {}
    for f in glob.glob(os.path.join(prefix, "conda-meta", "*.json")):
        try:
            n, v, b = os.path.basename(f)[:-5].rsplit("-", 2)
        except ValueError:
            continue
        pkgs[conda_norm(n)] = {"name": n, "version": v, "by": "conda", "json": f}
    sites = {os.path.realpath(p) for p in glob.glob(os.path.join(prefix, "lib", "python3*", "site-packages"))}
    for site in sorted(sites):
        for d in glob.glob(os.path.join(site, "*.dist-info")):
            inst = os.path.join(d, "INSTALLER")
            if not os.path.exists(inst) or open(inst).read().strip() != "pip":
                continue
            n, v = os.path.basename(d)[: -len(".dist-info")].rsplit("-", 1)
            meta = os.path.join(d, "METADATA")
            if os.path.exists(meta):
                for mline in open(meta, errors="replace"):
                    if mline.startswith("Name:"):
                        n = mline.split(":", 1)[1].strip()
                        break
                    if not mline.strip():
                        break
            key = conda_norm(n)
            rec = {"name": n, "version": v, "by": "pip",
                   "requested": os.path.exists(os.path.join(d, "REQUESTED")),
                   "installed_at": datetime.datetime.fromtimestamp(os.path.getmtime(inst))}
            if key in pkgs and pkgs[key]["by"] == "conda":
                rec["over_conda"] = pkgs[key]
            pkgs[key] = rec
    return pkgs


def conda_env_spec(env, root=None):
    """Explicit requests of an env, like `conda env export --from-history` plus pip REQUESTED.

    Returns {'name','channels','conda','pip','transactions','first_specs','created','updated'}.
    Later specs for the same package replace earlier ones; '# remove specs' drop them.
    """
    path = os.path.join(conda_envs_root(root), env, "conda-meta", "history")
    channels, specs, first, dates, ntx = [], {}, None, [], 0
    for line in open(path).read().splitlines():
        if line.startswith("==>"):
            ntx += 1
            m = re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line)
            if m:
                dates.append(m.group(0))
        elif line.startswith("# cmd:"):
            toks = line.split()
            for i, t in enumerate(toks[:-1]):
                if t in ("-c", "--channel") and toks[i + 1] not in channels:
                    channels.append(toks[i + 1])
        elif line.startswith("# update specs:") or line.startswith("# remove specs:"):
            try:
                items = json.loads(line.split(":", 1)[1])
            except ValueError:
                continue
            for s in items:
                key = conda_norm(conda_spec_name(s))
                specs.pop(key, None)
                if line.startswith("# update"):
                    specs[key] = s
            if first is None and line.startswith("# update"):
                first = list(items)
    inst = conda_installed(env, root)
    pip = sorted(v["name"] for v in inst.values() if v["by"] == "pip" and v.get("requested"))
    return {"name": env, "channels": channels or ["conda-forge"], "conda": list(specs.values()),
            "pip": pip, "transactions": ntx, "first_specs": first or [],
            "created": dates[0] if dates else None, "updated": dates[-1] if dates else None}


def conda_yaml_item(s):
    """Quote a YAML list item unless it is plainly safe."""
    return s if re.fullmatch(r"[A-Za-z0-9_.\-=<>!*,]+", s) else json.dumps(s)


def conda_env_yml(env, path=None, versions="comment", root=None):
    """Write a from-history style environment.yml for `env`; return the text.

    versions: 'none' (requests only), 'comment' (append '# installed: X'), or
    'pin' (unconstrained requests get '=X' / '==X' from what is installed).
    `path` may be a file or a directory (then '<dir>/environment.yml').
    """
    spec, inst = conda_env_spec(env, root), conda_installed(env, root)
    lines = ["# Generated by conda-env-hygiene from conda-meta/history on " + datetime.date.today().isoformat() + ".",
             "# Explicit requests only; solver-chosen dependencies are left out (cf. --from-history).",
             "name: " + env, "channels:"]
    lines += ["  - " + c for c in spec["channels"]] + ["  - nodefaults", "dependencies:"]
    deps = list(spec["conda"])
    if spec["pip"] and "pip" not in {conda_norm(conda_spec_name(d)) for d in deps}:
        deps.append("pip")

    def entry(s, op, indent):
        rec = inst.get(conda_norm(conda_spec_name(s)))
        bare = conda_spec_name(s) == s.split("::")[-1].strip()
        if versions == "pin" and rec and bare:
            s = s + op + rec["version"]
        tail = "  # installed: " + rec["version"] if versions == "comment" and rec else ""
        return indent + "- " + conda_yaml_item(s) + tail

    lines += [entry(d, "=", "  ") for d in deps]
    if spec["pip"]:
        lines.append("  - pip:")
        lines += [entry(p, "==", "      ") for p in spec["pip"]]
    text = "\n".join(lines) + "\n"
    if path:
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            path = os.path.join(path, "environment.yml")
        with open(path, "w") as fh:
            fh.write(text)
    return text


def conda_find(packages, root=None):
    """Which envs already have `packages`? Rows sorted by coverage, best first."""
    want = [conda_norm(p) for p in packages]
    rows = []
    for env in conda_env_names(root):
        inst = conda_installed(env, root)
        has = {p: inst[p]["version"] + " (" + inst[p]["by"] + ")" for p in want if p in inst}
        rows.append({"env": env, "has": has, "lacks": [p for p in want if p not in inst],
                     "starter": env in STARTER_ENVS})
    rows.sort(key=lambda r: (-len(r["has"]), r["starter"], r["env"]))
    return rows


def conda_env_audit(env, root=None, check_channels=False):
    """Hygiene findings for one env: list of (level, message), level in ok/info/warn.

    check_channels=True also asks conda-forge/bioconda (network, cached 24 h) which
    explicit pip installs had a conda build for this platform.
    """
    spec, inst = conda_env_spec(env, root), conda_installed(env, root)
    out = []
    added = [s for s in spec["conda"] if s not in spec["first_specs"]]
    if env in STARTER_ENVS and (added or spec["pip"]):
        out.append(("warn", "starter env changed since creation: conda " + str(added) + ", pip " + str(spec["pip"])))
    if spec["transactions"] > 1:
        out.append(("info", str(spec["transactions"] - 1) + " conda install(s) after creation; a rebuild from the merged spec solves everything at once"))
    pip_times = [v["installed_at"] for v in inst.values() if v["by"] == "pip"]
    if pip_times and spec["updated"]:
        last = datetime.datetime.strptime(spec["updated"], "%Y-%m-%d %H:%M:%S")
        if last > min(pip_times):
            out.append(("warn", "conda install on " + spec["updated"] + " ran after pip installs (first " + min(pip_times).strftime("%Y-%m-%d %H:%M") + "); conda cannot see pip packages, rebuild instead"))
    for v in inst.values():
        c = v.get("over_conda")
        if c and any("site-packages/" in f for f in json.load(open(c["json"])).get("files", [])):
            out.append(("warn", "pip " + v["name"] + " " + v["version"] + " overwrote conda's " + c["name"] + " " + c["version"]))
    if "python" in inst and not any(conda_spec_name(s) == "python" and s != "python" for s in spec["conda"]):
        out.append(("info", "python version not pinned in the requests"))
    if check_channels and spec["pip"]:
        found = conda_channel_lookup(spec["pip"], channels=tuple(spec["channels"]), subdir=conda_env_subdir(env, root))
        have = [p + " (" + v["channel"] + " " + v["version"] + ")" for p, v in found.items()
                if v and conda_norm(p) not in PIP_PREFERRED]
        if have:
            out.append(("warn", "pip-installed but available from conda: " + ", ".join(have)))
    return out or [("ok", "no findings")]


def conda_env_subdir(env, root=None):
    """Platform subdir of an env ('osx-64', 'osx-arm64', 'linux-64', ...), from its records."""
    for f in sorted(glob.glob(os.path.join(conda_envs_root(root), env, "conda-meta", "*.json"))):
        sub = json.load(open(f)).get("subdir")
        if sub and sub != "noarch":
            return sub
    return None


def conda_channel_lookup(names, channels=("conda-forge", "bioconda"), subdir=None, cache_dir=None, max_age_h=24):
    """Is there a conda build? {name: {'channel','version'} or None}, first channel wins.

    Reads https://conda.anaconda.org/CHANNEL/channeldata.json (the channel's own index;
    api.anaconda.org is not reachable from the sandbox), cached as a slim JSON in
    cache_dir (default ./.conda-channeldata). A package counts only if it has a
    noarch build or one for `subdir` (default: the subdir of the running env).
    Names are matched normalised; PyPI and conda names can still differ (torch/pytorch).
    """
    if cache_dir is None:
        cache_dir = os.path.join(os.getcwd(), ".conda-channeldata")
    if subdir is None:
        subdir = conda_env_subdir(os.path.basename(sys.base_prefix), os.path.dirname(sys.base_prefix))
    os.makedirs(cache_dir, exist_ok=True)
    result = {n: None for n in names}
    for ch in channels:
        cache = os.path.join(cache_dir, ch + ".json")
        if not os.path.exists(cache) or time.time() - os.path.getmtime(cache) > max_age_h * 3600:
            req = urllib.request.Request("https://conda.anaconda.org/" + ch + "/channeldata.json",
                                         headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            slim = {conda_norm(k): [v.get("version"), v.get("subdirs", [])]
                    for k, v in json.loads(raw)["packages"].items()}
            with open(cache, "w") as fh:
                json.dump(slim, fh)
        index = json.load(open(cache))
        for n in names:
            rec = index.get(conda_norm(n))
            if result[n] is None and rec and ("noarch" in rec[1] or subdir is None or subdir in rec[1]):
                result[n] = {"channel": ch, "version": rec[0]}
    return result


def conda_yml_to_create(path):
    """Read an environment.yml into manage_environments / manage_packages arguments.

    Returns {'name','channels','python_version','packages','pip'}; python_version is
    taken from a 'python=X.Y' request and removed from packages. Handles the plain
    list layout conda and this skill write (no anchors, no flow lists).
    """
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        path = os.path.join(path, "environment.yml")
    out = {"name": None, "channels": [], "python_version": None, "packages": [], "pip": []}
    section, pip_indent = None, -1
    for raw in open(path):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if not line.startswith(" ") and ":" in line:
            key, val = line.split(":", 1)
            section = key.strip()
            if section == "name":
                out["name"] = val.strip().strip("'\"")
            continue
        item, indent = line.strip(), len(line) - len(line.lstrip())
        if section == "pip" and indent <= pip_indent:
            section = "dependencies"
        if item == "- pip:":
            section, pip_indent = "pip", indent
            continue
        if not item.startswith("- "):
            continue
        item = item[2:].strip().strip("'\"")
        if section == "channels" and item != "nodefaults":
            out["channels"].append(item)
        elif section == "pip":
            out["pip"].append(item)
        elif section == "dependencies":
            m = re.fullmatch(r"python\s*[=]{1,2}\s*(\d+\.\d+)(\.[\d*]+)?", item)
            if m:
                out["python_version"] = m.group(1)
            else:
                out["packages"].append(item)
    return out
