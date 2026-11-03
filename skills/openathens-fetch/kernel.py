# openathens-fetch kernel helpers. See SKILL.md for the agent workflow.

OA_REDIRECTOR_DEFAULT = "https://go.openathens.net/redirector/library.harvard.edu?url="
OA_PROBE_URL_DEFAULT = "https://www.nature.com/articles/nchembio.1733"
OA_SESSION_DIR_DEFAULT = "~/oa-session"
OA_SKILL_NAME = "openathens-fetch"
OA_LAUNCHER_FILES = ("oa_login.py", "run.sh", "close-window.sh", "oa-login.command",
                     "oa-login-request.command",
                     "install-login-agent.command", "uninstall-login-agent.command",
                     "login-agent.plist.in")
OA_REDIRECT_CODES = (301, 302, 303, 307, 308)


# ---------------------------------------------------------------- session folder

def oa_session_dir(session_dir=None):
    """Session folder: argument, else $OA_SESSION_DIR, else ~/oa-session."""
    import os
    return os.path.expanduser(session_dir or os.environ.get("OA_SESSION_DIR")
                              or OA_SESSION_DIR_DEFAULT)


def oa_install(session_dir=None, redirector=None, probe_url=None, launcher_src=None):
    """Copy the launcher into the session folder and write/merge config.json. Idempotent.

    The folder must exist and be granted rw (the user creates it: mkdir -m 700). The venv
    is built by run.sh on the first double-click, outside the sandbox. launcher_src
    defaults to this skill's cached launcher/ folder next to the session workspace.
    """
    import os, json, shutil
    d = oa_session_dir(session_dir)
    src = launcher_src or os.path.join(os.getcwd(), "..", "..", "skills", OA_SKILL_NAME, "launcher")
    missing = [f for f in OA_LAUNCHER_FILES if not os.path.exists(os.path.join(src, f))]
    if missing:
        raise FileNotFoundError("launcher files not found in %s: %s (pass launcher_src=)"
                                % (os.path.abspath(src), missing))
    for name in OA_LAUNCHER_FILES:
        dst = os.path.join(d, name)
        shutil.copyfile(os.path.join(src, name), dst + ".part")
        os.chmod(dst + ".part", 0o700 if name.endswith((".py", ".sh", ".command")) else 0o600)
        os.replace(dst + ".part", dst)
    cfg = oa_load_json(os.path.join(d, "config.json")) or {}
    cfg["redirector"] = redirector or cfg.get("redirector") or OA_REDIRECTOR_DEFAULT
    cfg["probe_url"] = probe_url or cfg.get("probe_url") or OA_PROBE_URL_DEFAULT
    cfg.setdefault("idp_hosts", [])
    cfg.setdefault("login_timeout_s", 900)
    oa_write_private(os.path.join(d, "config.json"), cfg)
    return {"session_dir": d, "config": cfg,
            "venv_ready": os.path.exists(os.path.join(d, "venv", "bin", "python")),
            "agent_installed": os.path.exists(os.path.join(d, ".agent-installed"))}


def oa_load_json(path):
    import os, json
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def oa_write_private(path, obj):
    """Atomic JSON write, mode 0600."""
    import os, json
    fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(path + ".part", path)


def oa_jar_info(session_dir=None):
    """Safe summary of the cookie jar: age, count, domains. Never the values."""
    import os, time
    path = os.path.join(oa_session_dir(session_dir), "cookies.json")
    jar = oa_load_json(path)
    if jar is None:
        return {"exists": False, "path": path}
    now = time.time()
    return {"exists": True, "path": path,
            "captured_min_ago": round((now - jar["captured_at"]) / 60, 1),
            "refreshed_min_ago": round((now - jar.get("refreshed_at", jar["captured_at"])) / 60, 1),
            "n_cookies": len(jar["cookies"]), "idp_hosts": jar.get("idp_hosts", []),
            "silent_relogin": bool(jar.get("idp_hosts")),
            "domains": sorted({c["domain"] for c in jar["cookies"]})}


# ---------------------------------------------------------------- cookies

def oa_domain_matches(cookie_domain, host):
    d = cookie_domain.lstrip(".")
    return host == d or host.endswith("." + d)


def oa_is_identity_cookie(domain, idp_hosts):
    """True for OpenAthens cookies and cookies of the institution's login hosts."""
    return domain.lstrip(".").endswith("openathens.net") or any(
        oa_domain_matches(domain, h) for h in idp_hosts)


def oa_cookies_from_header(cookie_header, domain="login.openathens.net"):
    """Turn a DevTools 'name=value; name=value' Cookie line into jar-style cookies."""
    out = []
    for part in cookie_header.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            out.append({"name": name.strip(), "value": value.strip(), "domain": domain, "path": "/"})
    return out


def oa_requests_session(cookies):
    """requests.Session preloaded with jar-style (Playwright) cookie dicts."""
    import requests
    s = requests.Session()
    for c in cookies:
        exp = c.get("expires")
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"),
                      secure=bool(c.get("secure")), expires=int(exp) if exp and exp > 0 else None,
                      rest={"HttpOnly": None} if c.get("httpOnly") else {})
    return s


def oa_save_jar(session, jar, session_dir=None):
    """Write the session's identity cookies (incl. refreshed ones) back to the jar."""
    import os, time
    idp_hosts = jar.get("idp_hosts", [])
    jar = dict(jar, refreshed_at=time.time(), cookies=[
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
         "expires": c.expires or -1, "secure": bool(c.secure),
         "httpOnly": c.has_nonstandard_attr("HttpOnly")}
        for c in session.cookies if oa_is_identity_cookie(c.domain, idp_hosts)])
    oa_write_private(os.path.join(oa_session_dir(session_dir), "cookies.json"), jar)


# ---------------------------------------------------------------- network helpers

def oa_block_info(exc):
    """needs_allowlist dict if exc is a sandbox network-allowlist block, else None."""
    import re
    msg = str(exc)
    m = re.search(r"host ([A-Za-z0-9.\-]+) not on the allowlist", msg)
    if m:
        return {"status": "needs_allowlist", "host": m.group(1)}
    if "not on the allowlist" in msg or "Tunnel connection failed: 403" in msg:
        return {"status": "needs_allowlist", "host": None, "detail": msg[:200]}
    return None


def oa_resolve_article_url(doi_or_url):
    """DOI (or https URL) -> the publisher article landing URL."""
    import requests
    if doi_or_url.startswith("http"):
        return doi_or_url
    doi = doi_or_url.replace("https://doi.org/", "").replace("doi.org/", "").strip()
    return requests.get("https://doi.org/" + doi, allow_redirects=True, timeout=40).url


def oa_site(host):
    """Crude registrable domain: last two labels (sp.nature.com -> nature.com)."""
    return ".".join(host.split(".")[-2:])


# ---------------------------------------------------------------- fetch

def oa_fetch(doi_or_url, session_dir=None, cookies=None, redirector=None, outdir="articles_oa",
             pdf_url=None, check_only=False):
    """Fetch a paywalled PDF through the OpenAthens SAML chain with the user's session.

    Cookies come from the session folder's jar (default) or `cookies=` (list of jar-style
    dicts, e.g. from oa_cookies_from_header). With the jar, refreshed identity cookies are
    written back, and an expired OpenAthens session re-authenticates silently through the
    institution's login host while that session lives.
    check_only=True stops after the login chain (no landing page, no download).

    Returns {'status': ...}:
      ok              'path' = verified PDF (check_only: chain authenticated)
      login_required  an interactive login is needed (see SKILL.md login loop)
      needs_allowlist request_network_access(domain=res['host']), then retry
      no_pdf_link / not_pdf  authenticated but no PDF (bot wall, HTML only)
    """
    import os, re, time, html, requests
    from urllib.parse import urlparse, urljoin, quote
    d = oa_session_dir(session_dir)
    jar = None
    if cookies is None:
        jar = oa_load_json(os.path.join(d, "cookies.json"))
        if jar is None:
            return {"status": "login_required", "detail": "no cookie jar in " + d}
        cookies = jar["cookies"]
    idp_hosts = (jar or {}).get("idp_hosts", [])
    cfg = oa_load_json(os.path.join(d, "config.json")) or {}
    redirector = redirector or cfg.get("redirector") or OA_REDIRECTOR_DEFAULT
    s = oa_requests_session(cookies)
    has_idp_cookies = any(oa_domain_matches(c["domain"], h) for c in cookies for h in idp_hosts)
    chain = []

    def finish(status, **info):
        if jar is not None and status in ("ok", "no_pdf_link", "not_pdf"):
            oa_save_jar(s, jar, d)
        return dict(status=status, chain=chain, **info)

    try:
        article = oa_resolve_article_url(doi_or_url)
        article_site = oa_site(urlparse(article).netloc)
        # 1. redirector: a JS 'please wait' page; poll until it answers 201/30x + Location
        red = redirector + quote(article, safe="")
        r = s.get(red, allow_redirects=False, timeout=40)
        for _ in range(6):
            if r.status_code in OA_REDIRECT_CODES + (201,):
                break
            time.sleep(2.5)
            r = s.head(red, allow_redirects=False, timeout=40)
        url = r.headers.get("location")
        # 2. SAML chain: follow redirects, auto-submit SAMLResponse forms
        saml_posts = 0
        for _ in range(30):
            if not url:
                break
            host = urlparse(url).netloc
            if host in idp_hosts and not has_idp_cookies:
                return finish("login_required", detail="reached %s with no login cookies" % host)
            r = s.get(url, allow_redirects=False, timeout=40)
            chain.append((host, r.status_code))
            if "SAMLResponse" in r.text:
                action = html.unescape(re.search(r'<form[^>]*action="([^"]+)"', r.text).group(1))
                fields = {html.unescape(n): html.unescape(v) for n, v in
                          re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', r.text)}
                r = s.post(action, data=fields, allow_redirects=False, timeout=40)
                saml_posts += 1
                chain.append((urlparse(action).netloc, r.status_code))
                loc = r.headers.get("location")
                url = urljoin(action, loc) if loc else None
                continue
            loc = r.headers.get("location")
            if loc and r.status_code in OA_REDIRECT_CODES:
                url = urljoin(url, loc)
                continue
            if oa_site(host) != article_site:  # stopped on a login page, not the publisher
                return finish("login_required", detail="chain stopped at a login page on " + host)
            break
        if check_only:
            return finish("ok", check_only=True, saml_posts=saml_posts, article_url=article)
        # 3. landing page -> citation_pdf_url -> PDF
        land = s.get(article, timeout=40, allow_redirects=True)
        if pdf_url is None:
            m = (re.search(r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"', land.text)
                 or re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="citation_pdf_url"', land.text))
            pdf_url = html.unescape(m.group(1)) if m else None
        if pdf_url is None:
            return finish("no_pdf_link", landing_status=land.status_code, landing_url=land.url)
        rp = s.get(pdf_url, timeout=90, allow_redirects=True)
    except requests.exceptions.RequestException as e:
        block = oa_block_info(e)
        if block:
            return dict(block, chain=chain)
        raise
    if rp.content[:5] != b"%PDF-":
        return finish("not_pdf", got=rp.headers.get("content-type"), bytes=len(rp.content),
                      pdf_url=pdf_url)
    os.makedirs(outdir, exist_ok=True)
    stem = doi_or_url if not doi_or_url.startswith("http") else urlparse(article).path
    path = os.path.join(outdir, stem.strip("/").replace("/", "_") + ".pdf")
    with open(path, "wb") as fh:
        fh.write(rp.content)
    return finish("ok", path=path, bytes=len(rp.content), pdf_url=pdf_url, article_url=article)


def oa_session_status(session_dir=None):
    """'valid' | 'login_required' | 'needs_allowlist': runs the login chain for the probe only."""
    import os
    cfg = oa_load_json(os.path.join(oa_session_dir(session_dir), "config.json")) or {}
    r = oa_fetch(cfg.get("probe_url", OA_PROBE_URL_DEFAULT), session_dir=session_dir,
                 check_only=True)
    if r["status"] == "ok":
        r["status"] = "valid"
    return r


# ---------------------------------------------------------------- interactive login

def oa_request_login(session_dir=None):
    """Ask for a fresh login: writes login-request; returns what to tell the user."""
    import os, time
    d = oa_session_dir(session_dir)
    now = time.time()
    with open(os.path.join(d, "login-request"), "w") as fh:
        fh.write(str(now))
    agent = os.path.exists(os.path.join(d, ".agent-installed"))
    tell = ("A library login window will open on the Claude Science Mac by itself; finish the "
            "login there." if agent else
            "On the Claude Science Mac, double-click oa-login.command in %s and finish the "
            "library login in the window that opens." % d)
    return {"requested_at": now, "agent_installed": agent, "tell_user": tell}


def oa_wait_for_login(since, session_dir=None, timeout=900, poll=3):
    """Block until the launcher writes a jar newer than `since`, or reports a failure."""
    import os, time
    d = oa_session_dir(session_dir)
    t0 = time.time()
    while time.time() - t0 < timeout:
        jar = oa_load_json(os.path.join(d, "cookies.json"))
        if jar and jar["captured_at"] > since:
            return dict(oa_jar_info(d), status="captured")
        status_path = os.path.join(d, "last_login.json")
        if os.path.exists(status_path) and os.path.getmtime(status_path) > since:
            st = oa_load_json(status_path)
            if not st.get("ok"):
                return {"status": "login_failed", "error": st.get("error")}
        time.sleep(poll)
    return {"status": "timeout", "waited_s": round(time.time() - t0)}
