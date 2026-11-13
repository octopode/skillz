# paperpile-mcp: Paperpile MCP client with OAuth (PKCE, loopback client) and silent
# renewal from the user's Paperpile web-session cookie. Stdlib only. Secrets live in the
# session folder (mode 0600) or the PAPERPILE_SESSION credential and are never printed.
import os
import re
import json
import time
import base64
import hashlib
import secrets
import urllib.request
import urllib.parse
import urllib.error

PP_MCP_URL = "https://mcp.paperpile.com"
PP_AUTH_BASE = "https://api.paperpile.com"
PP_DEFAULT_PORT = 38791
PP_CLIENT_NAME = "paperpile-mcp personal client"
PP_PROTOCOL = "2025-06-18"
PP_COOKIE_ENV = "PAPERPILE_SESSION"
PP_WORKFLOW_REPO = "Paperpile/agent-plugins"
PP_WRITE_TOOLS = ("update_references", "update_collections", "highlight_text",
                  "save_to_library", "send_paperpile_feedback")


# ---------- session folder and files ----------

def pp_session_dir(session_dir=None):
    d = session_dir or os.environ.get("PP_SESSION_DIR") or os.path.expanduser("~/paperpile-session")
    if not os.path.isdir(d):
        raise RuntimeError(f"Paperpile session folder {d} is missing: ask the user to run "
                           f"`mkdir -m 700 {d}` and grant it rw, or set PP_SESSION_DIR.")
    return d


def pp_read_json(d, name, default=None):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return json.load(f)


def pp_write_json(d, name, obj, private=True):
    p = os.path.join(d, name)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(p + ".tmp", p)
    if private:
        os.chmod(p, 0o600)
    return p


def pp_config(session_dir=None):
    cfg = pp_read_json(pp_session_dir(session_dir), "config.json", {}) or {}
    cfg.setdefault("port", PP_DEFAULT_PORT)
    cfg.setdefault("granted_scope", "read_write")
    return cfg


def pp_rm(d, *names):
    for n in names:
        p = os.path.join(d, n)
        if os.path.exists(p):
            os.remove(p)


# ---------- HTTP ----------

def pp_http(method, url, body=None, headers=None, form=False, timeout=60, follow=True):
    """Return (status, headers_lowercase, bytes). Raises RuntimeError('needs_allowlist:<host>')."""
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    handlers = [] if follow else [pp_no_redirect_handler()]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, pp_hdrs(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, pp_hdrs(e.headers), e.read()
    except urllib.error.URLError as e:
        msg = str(e.reason)
        if "403" in msg or "Tunnel connection failed" in msg or "refused" in msg.lower():
            raise RuntimeError("needs_allowlist:" + urllib.parse.urlparse(url).hostname)
        raise


def pp_hdrs(h):
    out = {}
    for k, v in (h.items() if h else []):
        k = k.lower()
        out[k] = out[k] + "\n" + v if k in out and k == "set-cookie" else v
    if h is not None and hasattr(h, "get_all"):
        sc = h.get_all("Set-Cookie") or []
        if sc:
            out["set-cookie"] = "\n".join(sc)
    return out


def pp_no_redirect_handler():
    h = urllib.request.HTTPRedirectHandler()
    h.redirect_request = lambda *a, **k: None
    return h


def pp_status_from_error(e):
    s = str(e)
    if s.startswith("needs_allowlist:"):
        return {"status": "needs_allowlist", "host": s.split(":", 1)[1]}
    if s.startswith("login_required"):
        return {"status": "login_required", "detail": s}
    return {"status": "error", "detail": s[:500]}


# ---------- OAuth client + PKCE ----------

def pp_register(session_dir=None, force=False):
    """Dynamic client registration (public client, PKCE, loopback redirect). Stored in client.json."""
    d = pp_session_dir(session_dir)
    redirect = f"http://127.0.0.1:{int(pp_config(d)['port'])}/callback"
    cl = pp_read_json(d, "client.json")
    if cl and not force and cl.get("redirect_uri") == redirect:
        return {"status": "ok", "registered": False}
    st, _, raw = pp_http("POST", PP_AUTH_BASE + "/api/oauth/register", {
        "client_name": PP_CLIENT_NAME, "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    if st not in (200, 201):
        return {"status": "error", "detail": f"registration HTTP {st}"}
    pp_write_json(d, "client.json", {"client_id": json.loads(raw)["client_id"],
                                     "redirect_uri": redirect, "registered_at": time.time()})
    return {"status": "ok", "registered": True}


def pp_new_pending(d):
    """Fresh PKCE verifier + state in pending.json; returns the authorize URL."""
    reg = pp_register(d)
    if reg["status"] != "ok":
        raise RuntimeError("client registration failed: " + reg.get("detail", ""))
    cl = pp_read_json(d, "client.json")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    pp_write_json(d, "pending.json", {"state": state, "verifier": verifier,
                                      "redirect_uri": cl["redirect_uri"], "created": time.time()})
    q = {"response_type": "code", "client_id": cl["client_id"], "redirect_uri": cl["redirect_uri"],
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
         "resource": PP_MCP_URL}
    return PP_AUTH_BASE + "/api/oauth/authorize?" + urllib.parse.urlencode(q)


def pp_exchange(d, code, state):
    pend = pp_read_json(d, "pending.json")
    if not pend:
        return {"status": "error", "detail": "no pending sign-in"}
    if state != pend["state"]:
        return {"status": "login_failed", "detail": "state mismatch (stale or foreign callback)"}
    cl = pp_read_json(d, "client.json")
    st, _, raw = pp_http("POST", PP_AUTH_BASE + "/api/oauth/token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": pend["redirect_uri"],
        "client_id": cl["client_id"], "code_verifier": pend["verifier"], "resource": PP_MCP_URL},
        form=True)
    if st != 200:
        return {"status": "login_failed", "detail": f"token exchange HTTP {st}"}
    j = json.loads(raw)
    now = time.time()
    pp_write_json(d, "tokens.json", {
        "access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
        "expires_at": now + float(j.get("expires_in") or 3600),
        "token_type": j.get("token_type", "Bearer"), "obtained_at": now})
    pp_rm(d, "pending.json", "mcp_session.json")
    return {"status": "captured"}


# ---------- cookie jar (Paperpile web session) ----------

def pp_cookie_parse(header):
    header = (header or "").strip()
    if header.lower().startswith("cookie:"):
        header = header[7:]
    jar = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                jar[k.strip()] = v.strip()
    return jar


def pp_set_cookie(header=None, session_dir=None):
    """Seed the jar from a Cookie header, or (header=None) from the PAPERPILE_SESSION credential
    env var, which exists only in cells that declare credentials=["PAPERPILE_SESSION"]."""
    d = pp_session_dir(session_dir)
    if header is None:
        header = os.environ.get(PP_COOKIE_ENV)
        if not header:
            return {"status": "error", "detail": f"declare credentials=['{PP_COOKIE_ENV}'] on this cell"}
    jar = pp_cookie_parse(header)
    if "plack_session" not in jar:
        return {"status": "error", "detail": "no plack_session cookie in the header"}
    pp_write_json(d, "cookies.json", {"cookies": jar, "updated_at": time.time(),
                                      "seeded_plack": jar.get("plack_session")})
    return {"status": "ok", "cookie_names": sorted(jar)}


def pp_cookie_jar(d):
    env = os.environ.get(PP_COOKIE_ENV)
    stored = pp_read_json(d, "cookies.json") or {}
    if env and pp_cookie_parse(env).get("plack_session") != stored.get("seeded_plack"):
        jar = pp_cookie_parse(env)
        pp_write_json(d, "cookies.json", {"cookies": jar, "updated_at": time.time(),
                                          "seeded_plack": jar.get("plack_session")})
        return jar
    return stored.get("cookies") or {}


def pp_cookie_update(d, jar, set_cookie):
    changed = False
    for line in (set_cookie or "").split("\n"):
        m = re.match(r"\s*([^=;\s]+)=([^;]*)", line)
        if m and jar.get(m.group(1)) != m.group(2):
            jar[m.group(1)] = m.group(2)
            changed = True
    if changed:
        stored = pp_read_json(d, "cookies.json") or {}
        stored.update({"cookies": jar, "updated_at": time.time()})
        pp_write_json(d, "cookies.json", stored)


def pp_renew_silent(session_dir=None):
    """New access token from the web-session cookie: authorize -> consent POST -> code -> token."""
    d = pp_session_dir(session_dir)
    jar = pp_cookie_jar(d)
    if "plack_session" not in jar:
        return {"status": "login_required", "detail": "no Paperpile session cookie stored"}
    ck = lambda: "; ".join(f"{k}={v}" for k, v in jar.items())
    url = pp_new_pending(d)
    st, h, _ = pp_http("GET", url, headers={"Cookie": ck()}, follow=False)
    pp_cookie_update(d, jar, h.get("set-cookie"))
    loc = h.get("location", "")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    if "/sign-in" in loc or "csrf_token" not in q:
        return {"status": "login_required", "detail": f"session cookie no longer accepted (HTTP {st}); "
                "the user must refresh the PAPERPILE_SESSION credential"}
    body = {k: q[k] for k in ("response_type", "client_id", "redirect_uri", "code_challenge",
                              "code_challenge_method", "state", "resource") if k in q}
    body.update(csrf_token=q["csrf_token"], granted_scope=pp_config(d)["granted_scope"])
    st, h, raw = pp_http("POST", PP_AUTH_BASE + "/api/oauth/authorize/continue", body, follow=False,
                         headers={"Cookie": ck(), "Accept": "application/json",
                                  "Origin": "https://paperpile.com", "Referer": "https://paperpile.com/"})
    pp_cookie_update(d, jar, h.get("set-cookie"))
    code = None
    if st in (200, 201) and raw.strip().startswith(b"{"):
        code = json.loads(raw).get("code")
    elif h.get("location", "").startswith("http://127.0.0.1"):
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(h["location"]).query)).get("code")
    if not code:
        return {"status": "login_required", "detail": f"consent step failed (HTTP {st})"}
    res = pp_exchange(d, code, body["state"])
    if res["status"] == "captured":
        pp_write_json(d, "last_renewal.json", {"at": time.time(), "how": "silent"}, private=False)
    return res


# ---------- interactive fallback (paste-back) ----------

def pp_request_login(session_dir=None):
    """Browser sign-in. The user opens the link, approves, and pastes the failing 127.0.0.1 address."""
    d = pp_session_dir(session_dir)
    url = pp_new_pending(d)
    port = pp_config(d)["port"]
    tell = (f"**Paperpile sign-in needed.** Open [this sign-in link]({url}), sign in and click Allow. "
            f"The last page (`127.0.0.1:{port}/callback?...`) will not load: copy its full address "
            f"from the URL bar and paste it here.")
    return {"status": "requested", "url": url, "tell_user": tell}


def pp_login_finish(callback, session_dir=None):
    """Complete a browser sign-in from the pasted callback address. Never echoes the code."""
    d = pp_session_dir(session_dir)
    query = urllib.parse.urlparse(callback).query if "://" in callback else callback.lstrip("?")
    q = dict(urllib.parse.parse_qsl(query))
    if "error" in q:
        return {"status": "login_failed", "detail": q.get("error_description") or q["error"]}
    if "code" not in q:
        return {"status": "login_failed", "detail": "no code in callback"}
    return pp_exchange(d, q["code"], q.get("state"))


# ---------- tokens ----------

def pp_token(session_dir=None, force_renew=False):
    """Valid access token; renews silently from the cookie. Raises RuntimeError('login_required...')."""
    d = pp_session_dir(session_dir)
    tok = pp_read_json(d, "tokens.json")
    if tok and not force_renew and tok["expires_at"] - 120 > time.time():
        return tok["access_token"]
    res = pp_renew_silent(d)
    if res["status"] != "captured":
        raise RuntimeError("login_required: " + res.get("detail", res["status"]))
    return pp_read_json(d, "tokens.json")["access_token"]


def pp_token_info(session_dir=None):
    """Safe summary, never secret values."""
    d = pp_session_dir(session_dir)
    tok = pp_read_json(d, "tokens.json") or {}
    ck = pp_read_json(d, "cookies.json") or {}
    ren = pp_read_json(d, "last_renewal.json") or {}
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None
    return {"token_expires_in_s": int(tok["expires_at"] - time.time()) if tok else None,
            "cookie_stored": "plack_session" in (ck.get("cookies") or {}),
            "cookie_updated": fmt(ck.get("updated_at")), "last_silent_renewal": fmt(ren.get("at")),
            "granted_scope": pp_config(d)["granted_scope"]}


def pp_logout(session_dir=None, forget_cookie=False):
    d = pp_session_dir(session_dir)
    pp_rm(d, "tokens.json", "mcp_session.json", "pending.json", *(["cookies.json"] if forget_cookie else []))
    return {"status": "logged_out", "cookie_forgotten": forget_cookie}


# ---------- MCP (streamable HTTP) ----------

def pp_parse_rpc(hdrs, raw, want_id):
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in hdrs.get("content-type", ""):
        msgs = []
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(l[5:].lstrip() for l in block.split("\n") if l.startswith("data:"))
            if data:
                try:
                    msgs.append(json.loads(data))
                except ValueError:
                    pass
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == want_id:
                return m
        return msgs[-1] if msgs else {}
    return json.loads(text) if text.strip() else {}


def pp_post_rpc(token, payload, sid=None, proto=None):
    h = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    if proto:
        h["MCP-Protocol-Version"] = proto
    return pp_http("POST", PP_MCP_URL, payload, headers=h, timeout=180)


def pp_mcp_init(d, token):
    st, h, raw = pp_post_rpc(token, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": PP_PROTOCOL, "capabilities": {},
        "clientInfo": {"name": "paperpile-mcp", "version": "0.2"}}})
    if st == 401:
        return st, None
    if st != 200:
        raise RuntimeError(f"MCP initialize HTTP {st}")
    res = pp_parse_rpc(h, raw, 0).get("result", {})
    sess = {"sid": h.get("mcp-session-id"), "proto": res.get("protocolVersion", PP_PROTOCOL),
            "server": res.get("serverInfo"), "instructions": res.get("instructions")}
    pp_post_rpc(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sess["sid"], sess["proto"])
    pp_write_json(d, "mcp_session.json", sess, private=False)
    return 200, sess


def pp_rpc(method, params=None, session_dir=None):
    """One JSON-RPC call; renews the token on 401 and re-initialises a lost session."""
    d = pp_session_dir(session_dir)
    token = pp_token(d)
    for attempt in range(3):
        sess = pp_read_json(d, "mcp_session.json")
        if not sess:
            st, sess = pp_mcp_init(d, token)
            if st == 401:
                token = pp_token(d, force_renew=True)
                continue
        rid = int(time.time() * 1000) % 10**9
        st, h, raw = pp_post_rpc(token, {"jsonrpc": "2.0", "id": rid, "method": method,
                                         "params": params or {}}, sess.get("sid"), sess.get("proto"))
        if st == 401:
            token = pp_token(d, force_renew=True)
            continue
        if st in (400, 404) and sess.get("sid"):
            pp_rm(d, "mcp_session.json")
            continue
        if st not in (200, 202):
            raise RuntimeError(f"MCP {method} HTTP {st}: {raw[:300].decode(errors='replace')}")
        msg = pp_parse_rpc(h, raw, rid)
        if "error" in msg:
            raise RuntimeError(f"MCP {method} error: {msg['error']}")
        return msg.get("result", {})
    raise RuntimeError("login_required: server kept rejecting the token")


def pp_session_status(session_dir=None):
    """valid / login_required / needs_allowlist (MCP ping; renews silently if needed)."""
    try:
        pp_rpc("ping", {}, session_dir)
        return {"status": "valid", **pp_token_info(session_dir)}
    except RuntimeError as e:
        return pp_status_from_error(e)


def pp_tools(session_dir=None, full=False):
    """Tools with a 'writes' flag (Paperpile sends no annotations; PP_WRITE_TOOLS is hard-coded)."""
    try:
        out, cursor = [], None
        while True:
            r = pp_rpc("tools/list", {"cursor": cursor} if cursor else {}, session_dir)
            out += r.get("tools", [])
            cursor = r.get("nextCursor")
            if not cursor:
                break
    except RuntimeError as e:
        return pp_status_from_error(e)
    pp_write_json(pp_session_dir(session_dir), "tools.json", out, private=False)
    if full:
        return out
    return [{"name": t["name"], "writes": t["name"] in PP_WRITE_TOOLS or t["name"].startswith(("update_", "save_", "delete_")),
             "required": (t.get("inputSchema") or {}).get("required"),
             "summary": (t.get("description") or "").strip().split("\n")[0][:160]} for t in out]


def pp_call(tool, rationale, session_dir=None, **arguments):
    """Call one tool. Paperpile requires a short 'rationale' on every call.
    Returns {'status','is_error','text','structured','content'}."""
    try:
        r = pp_rpc("tools/call", {"name": tool, "arguments": {"rationale": rationale, **arguments}}, session_dir)
    except RuntimeError as e:
        return pp_status_from_error(e)
    content = r.get("content") or []
    text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    return {"status": "ok", "is_error": bool(r.get("isError")), "text": text,
            "structured": r.get("structuredContent"), "content": content}


def pp_workflow(name=None):
    """Paperpile's own workflow guides (their agent-plugin skills) from GitHub; name=None lists them."""
    base = f"https://api.github.com/repos/{PP_WORKFLOW_REPO}/contents/claude-code/skills"
    if name is None:
        st, _, raw = pp_http("GET", base, headers={"Accept": "application/vnd.github+json"})
        return [x["name"] for x in json.loads(raw) if x.get("type") == "dir"] if st == 200 else {"status": "error", "http": st}
    st, _, raw = pp_http("GET", f"{base}/{name}/SKILL.md", headers={"Accept": "application/vnd.github.raw"})
    return raw.decode() if st == 200 else {"status": "error", "http": st}


# ---------- structured access ----------

def pp_json(tool, rationale, session_dir=None, **args):
    """Call a tool with output_format=JSON and parse it. Raises RuntimeError on failure."""
    r = pp_call(tool, rationale, session_dir=session_dir, output_format="JSON", **args)
    if r["status"] != "ok":
        raise RuntimeError(f"{r['status']}: {r.get('detail') or r.get('host') or ''}")
    if r["is_error"]:
        raise RuntimeError("tool_error: " + r["text"][:300])
    return json.loads(r["text"]) if r["text"].strip() else {}


def pp_norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pp_doi(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", s)
    return s or None


def pp_ref_key(ref):
    """Primary identity of a reference: DOI, else normalised title."""
    d = pp_doi(ref.get("doi"))
    return ("doi:" + d) if d else ("title:" + pp_norm(ref.get("title")))


def pp_ref_keys(ref):
    """All identities (DOI and title) so entries with and without a DOI still match."""
    ks = set()
    if pp_doi(ref.get("doi")):
        ks.add("doi:" + pp_doi(ref.get("doi")))
    if pp_norm(ref.get("title")):
        ks.add("title:" + pp_norm(ref.get("title")))
    return ks


def pp_catalog(dois, session_dir=None):
    """{doi: catalog record} from Paperpile's catalog (lookup_papers; saves nothing)."""
    out = {}
    for i in range(0, len(dois), 25):
        res = pp_json("lookup_papers", "Get catalog metadata to match library entries stored without a DOI",
                      session_dir, dois=dois[i:i + 25])
        for x in res.get("papers", []):
            if pp_doi(x.get("doi")):
                out[pp_doi(x["doi"])] = x
    return out


def pp_first_family(ref):
    a = (ref.get("author") or [{}])[0] or {}
    return pp_norm(a.get("family") or a.get("literal") or "")


def pp_same_work(lib, cat):
    """Confident identity: same normalised title, and same year and first author when both known."""
    if not pp_norm(lib.get("title")) or pp_norm(lib.get("title")) != pp_norm(cat.get("title")):
        return False
    ly, cy = (lib.get("published") or "")[:4], (cat.get("published") or "")[:4]
    lf, cf = pp_first_family(lib), pp_first_family(cat)
    return not (ly and cy and ly != cy) and not (lf and cf and lf != cf)


def pp_crossref(doi):
    """Independent check: Crossref's record for a DOI, in Paperpile-like field names (or None)."""
    st, _, raw = pp_http("GET", "https://api.crossref.org/works/" + urllib.parse.quote(pp_doi(doi) or "", safe="/"),
                         headers={"Accept": "application/json"}, timeout=30)
    if st != 200:
        return None
    m = json.loads(raw).get("message", {})
    dp = ((m.get("issued") or m.get("published") or {}).get("date-parts") or [[None]])[0]
    return {"doi": pp_doi(m.get("DOI")), "title": (m.get("title") or [""])[0],
            "published": str(dp[0]) if dp and dp[0] else "",
            "author": [{"family": a.get("family") or a.get("name")} for a in (m.get("author") or [])],
            "journal": (m.get("container-title") or [None])[0], "volume": m.get("volume"),
            "issue": m.get("issue"), "pages": m.get("page"), "publisher": m.get("publisher")}


def pp_enrich(ref, cat, session_dir=None):
    """Fill EMPTY bibliographic fields of a library entry, only when the library entry, Paperpile's
    catalog record and Crossref (independent) all agree on title, year and first author.
    Never overwrites existing values, never touches title/authors; reads back to verify.
    Returns {'filled': [...], 'skipped': reason or None}."""
    doi = pp_doi(cat.get("doi"))
    if not doi or not pp_same_work(ref, cat):
        return {"filled": [], "skipped": "catalog record does not match the library entry"}
    try:
        cr = pp_crossref(doi)
    except RuntimeError as e:
        return {"filled": [], "skipped": "Crossref unreachable (" + str(e)[:60] + ")"}
    if not cr or not pp_same_work(ref, cr) or not pp_same_work(cat, cr):
        return {"filled": [], "skipped": "Crossref does not confirm the match"}
    full = pp_lookup(citekeys=[ref["citekey"]], session_dir=session_dir) if ref.get("citekey") else []
    ref = full[0] if full else ref
    if not ref.get("id"):
        return {"filled": [], "skipped": "library entry id unknown"}
    src = {"doi": cr.get("doi"), "journal": cr.get("journal") or cat.get("journal"),
           "volume": cr.get("volume") or cat.get("volume"), "issue": cr.get("issue") or cat.get("issue"),
           "pages": cr.get("pages") or cat.get("pages"), "publisher": cr.get("publisher") or cat.get("publisher"),
           "pmid": cat.get("pmid"), "abstract": cat.get("abstract")}
    fill = {f: v for f, v in src.items() if v and not ref.get(f)}
    if not fill:
        return {"filled": [], "skipped": None}
    pp_json("update_references", "Fill in missing bibliographic fields (user's standing request; "
            "empty fields only, verified against Crossref)", session_dir,
            updates=[{"id": ref["id"], "metadata": fill}])
    after = pp_lookup(citekeys=[ref["citekey"]], session_dir=session_dir)
    ok = [f for f in fill if after and after[0].get(f)]
    return {"filled": sorted(ok), "skipped": None if len(ok) == len(fill) else
            "not stored: " + ", ".join(sorted(set(fill) - set(ok)))}


def pp_lookup(dois=None, citekeys=None, session_dir=None):
    """References already in the personal library, matched by DOI and/or citekey."""
    out = []
    dois = [d for d in (pp_doi(x) for x in (dois or [])) if d]
    for i in range(0, max(len(dois), len(citekeys or [])), 50):
        args = {}
        if dois[i:i + 50]:
            args["dois"] = dois[i:i + 50]
        if (citekeys or [])[i:i + 50]:
            args["citekeys"] = citekeys[i:i + 50]
        if args:
            out += pp_json("get_references_from_library", "Check whether these papers are already in the library",
                           session_dir, **args).get("data", [])
    return out


def pp_find_title(title, session_dir=None, year=None):
    """Library reference whose normalised title equals `title` (and year, if both are known)."""
    want = pp_norm(title)
    if not want:
        return None
    res = pp_json("search_library", "Check whether this paper is already in the library (title match)",
                  session_dir, q=title[:200], limit=5)
    for hit in res.get("data", []):
        ref = hit.get("reference") or {}
        ry = (ref.get("published") or "")[:4]
        if pp_norm(ref.get("title")) == want and not (year and ry and str(year)[:4] != ry):
            return ref
    return None


def pp_library_fulltext(doi=None, citekey=None, max_bytes=100000, session_dir=None):
    """Library-first full text. Returns status ok (text from the user's stored PDF), no_pdf or
    not_in_library. Only when it is not 'ok' may the web be searched for the full text."""
    refs = pp_lookup(dois=[doi] if doi else None, citekeys=[citekey] if citekey else None,
                     session_dir=session_dir)
    enriched = []
    if not refs and doi:
        cat = pp_catalog([pp_doi(doi)], session_dir).get(pp_doi(doi))
        hit = pp_find_title(cat.get("title"), session_dir, year=(cat.get("published") or "")[:4]) if cat else None
        if hit:
            refs = pp_lookup(citekeys=[hit["citekey"]], session_dir=session_dir) or [hit]
            if pp_same_work(refs[0], cat):
                enriched = pp_enrich(refs[0], cat, session_dir)
                refs = pp_lookup(citekeys=[refs[0]["citekey"]], session_dir=session_dir) or refs
            else:
                refs = []
    if not refs:
        return {"status": "not_in_library"}
    ref = refs[0]
    if not ref.get("files"):
        return {"status": "no_pdf", "citekey": ref.get("citekey")}
    r = pp_call("get_text_content", "Read the full text stored in the user's library",
                session_dir=session_dir, citekey=ref["citekey"], max_bytes=int(max_bytes))
    if r["status"] != "ok" or r["is_error"] or not r["text"].strip():
        return {"status": "no_pdf", "citekey": ref.get("citekey"), "detail": r.get("text", "")[:200]}
    try:
        j = json.loads(r["text"])
        pages = j.get("content") or []
        text = "\n\n".join(f"[page {p.get('page')}]\n{p.get('text', '')}" for p in pages)
        last = max([p.get("page") or 0 for p in pages] or [0])
        truncated = last < (j.get("total_pages") or 0) or any(
            p.get("end") is not None and p.get("page_bytes") and p["end"] < p["page_bytes"] for p in pages)
        return {"status": "ok", "citekey": ref["citekey"], "title": ref.get("title"), "text": text,
                "total_pages": j.get("total_pages"), "truncated": truncated, "file_id": j.get("file_id"),
                "enriched": enriched}
    except ValueError:
        return {"status": "ok", "citekey": ref["citekey"], "title": ref.get("title"), "text": r["text"],
                "truncated": None}


# ---------- project folders ----------

def pp_folders(session_dir=None):
    res = pp_json("get_collections_from_library", "List folders to find the project folder",
                  session_dir, include_shared_libraries=False)
    return [c for c in res.get("data", []) if c.get("collection_type") == "folder"
            and c.get("library_id", "personal") == "personal"]


def pp_project_folder(project, create=True, session_dir=None):
    """Folder for a Claude Science project. status: ok | created | confirm (close matches, ask the
    user, then pp_map_project_folder) | missing (create=False)."""
    d = pp_session_dir(session_dir)
    maps = pp_read_json(d, "project_folders.json", {}) or {}
    folders = pp_folders(d)
    by_id = {f["id"]: f for f in folders}
    if project in maps and maps[project] in by_id:
        f = by_id[maps[project]]
        return {"status": "ok", "folder_id": f["id"], "name": f["name"], "refs": f.get("reference_count")}
    exact = [f for f in folders if f["name"] == project]
    if exact:
        pp_map_project_folder(project, exact[0]["id"], d)
        return {"status": "ok", "folder_id": exact[0]["id"], "name": project, "refs": exact[0].get("reference_count")}
    p = pp_norm(project)
    close = [f for f in folders if p and (pp_norm(f["name"]) == p or
             (min(len(p), len(pp_norm(f["name"]))) >= 4 and (p in pp_norm(f["name"]) or pp_norm(f["name"]) in p)))]
    if close and not (project in maps and maps[project] == "__new__"):
        return {"status": "confirm", "project": project,
                "candidates": [{"folder_id": f["id"], "name": f["name"], "refs": f.get("reference_count")} for f in close]}
    if not create:
        return {"status": "missing", "project": project}
    pp_json("update_collections", f"Create the folder for Claude Science project '{project}'", d,
            updates=[{"action": "create", "type": "folder", "name": project}])
    made = [f for f in pp_folders(d) if f["name"] == project]
    if not made:
        raise RuntimeError("folder creation did not show up")
    pp_map_project_folder(project, made[0]["id"], d)
    return {"status": "created", "folder_id": made[0]["id"], "name": project, "refs": 0}


def pp_map_project_folder(project, folder_id, session_dir=None):
    """Remember the user's choice: folder_id of an existing folder, or '__new__' to create one
    named exactly like the project despite close matches."""
    d = pp_session_dir(session_dir)
    maps = pp_read_json(d, "project_folders.json", {}) or {}
    maps[project] = folder_id
    pp_write_json(d, "project_folders.json", maps, private=False)
    return {"status": "ok", "project": project, "folder_id": folder_id}


def pp_folder_refs(folder_id, session_dir=None):
    out, cursor = [], None
    while True:
        args = {"folder_id": folder_id, "limit": 100}
        if cursor:
            args["cursor"] = cursor
        res = pp_json("get_references_from_library", "List the references in the project folder", session_dir, **args)
        out += res.get("data", [])
        cursor = res.get("cursor")
        if not cursor:
            return out


# ---------- filing (never duplicates) ----------

def pp_file(folder_id, items, session_dir=None, note=None):
    """File sources into a folder without ever creating duplicates.
    items: list of dicts with 'doi' and/or 'title' and optional 'bibtex' (one entry) for DOI-less
    works. Existing library entries are only added to the folder; new ones are saved into it.
    Returns lists of citekeys/identifiers: already_in_folder, added_to_folder, saved_new, failed."""
    d = pp_session_dir(session_dir)
    rep = {"already_in_folder": [], "added_to_folder": [], "saved_new": [], "failed": [], "enriched": []}
    seen, uniq = set(), []
    for it in items:
        k = pp_ref_key(it)
        if k not in seen and k not in ("title:",):
            seen.add(k)
            uniq.append(it)
    dois = [pp_doi(it.get("doi")) for it in uniq if pp_doi(it.get("doi"))]
    existing = {pp_doi(r.get("doi")): r for r in pp_lookup(dois=dois, session_dir=d) if r.get("doi")}
    missing = [x for x in dois if x not in existing]
    cats = pp_catalog(missing, d) if missing else {}
    to_add, new_dois = [], []
    for it in uniq:
        doi = pp_doi(it.get("doi"))
        ref = existing.get(doi) if doi else pp_find_title(it.get("title"), d)
        if doi and not ref:          # entry may be stored without a DOI: match by title (+year, author)
            cat = cats.get(doi) or ({"title": it["title"], "doi": doi} if it.get("title") else None)
            hit = pp_find_title(cat.get("title"), d, year=(cat.get("published") or "")[:4]) if cat else None
            if hit:
                full = pp_lookup(citekeys=[hit["citekey"]], session_dir=d)
                ref = full[0] if full else hit
                if pp_same_work(ref, cat):
                    en = pp_enrich(ref, cat, d)
                    if en["filled"] or en["skipped"]:
                        rep["enriched"].append({"citekey": ref.get("citekey"), **en})
                else:
                    ref = None
        if ref:
            (rep["already_in_folder"] if folder_id in (ref.get("folders") or []) else to_add).append(ref)
        elif doi:
            new_dois.append(doi)
        elif it.get("bibtex"):
            res = pp_json("save_to_library", "File a source used in this project (no duplicate found)", d,
                          bibtex=it["bibtex"], folder_ids=[folder_id], skip_duplicates=True, note=note)
            rep["saved_new"].append(it.get("title") or "bibtex entry")
        else:
            rep["failed"].append({"item": it.get("title"), "why": "no DOI and no BibTeX"})
    seen_ids, dedup = set(), []
    for r in to_add:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            dedup.append(r)
    to_add = dedup
    rep["already_in_folder"] = sorted({r.get("citekey") for r in rep["already_in_folder"]})
    for i in range(0, len(to_add), 50):
        batch = to_add[i:i + 50]
        pp_json("update_references", "Add sources used in this project to its folder", d,
                updates=[{"id": r["id"], "add_folders": [folder_id]} for r in batch])
        rep["added_to_folder"] += [r.get("citekey") for r in batch]
    for i in range(0, len(new_dois), 25):
        batch = new_dois[i:i + 25]
        args = {"dois": batch, "folder_ids": [folder_id], "skip_duplicates": True, "wait_for_pdf": False}
        if note:
            args["note"] = note
        res = pp_json("save_to_library", "File sources used in this project (not yet in the library)", d, **args)
        by_doi = {pp_doi(x.get("doi")): x for x in (res.get("dois") or []) if isinstance(x, dict)}
        dup_ids = []
        for doi in batch:
            x = by_doi.get(doi, {})
            stt = str(x.get("status", "")).lower()
            if stt == "duplicate" and (x.get("existing") or {}).get("id"):
                dup_ids.append(x["existing"])            # raced into the library: just add to folder
            elif stt in ("error", "failed", "not_found", "invalid") or x.get("error"):
                rep["failed"].append({"doi": doi, "why": str(x.get("error") or x.get("message") or stt)[:200]})
            else:
                rep["saved_new"].append((x.get("reference") or {}).get("citekey") or x.get("citekey") or doi)
        if dup_ids:
            pp_json("update_references", "Add sources used in this project to its folder", d,
                    updates=[{"id": e["id"], "add_folders": [folder_id]} for e in dup_ids])
            rep["added_to_folder"] += [e.get("citekey") for e in dup_ids]
    return rep


# ---------- BibTeX ----------

def pp_bibtex(refs):
    kinds = {"article": "article", "book": "book", "chapter": "incollection", "conference": "inproceedings",
             "thesis": "phdthesis", "report": "techreport"}
    out = []
    for r in refs:
        f = {"title": r.get("title"), "journal": r.get("journal"), "year": (r.get("published") or "")[:4],
             "volume": r.get("volume"), "number": r.get("issue"), "pages": r.get("pages"),
             "doi": r.get("doi"), "url": r.get("url") if not r.get("doi") else None}
        f["author"] = " and ".join(
            (f"{a.get('family')}, {a.get('given')}" if a.get("given") else (a.get("family") or a.get("literal") or ""))
            for a in (r.get("author") or []))
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in f.items() if v)
        out.append(f"@{kinds.get(r.get('type'), 'misc')}{{{r.get('citekey') or r.get('id')},\n{body}\n}}")
    return "\n\n".join(out) + ("\n" if out else "")


def pp_bib_parse(text):
    """Minimal BibTeX reader: [{'key','doi','title','raw'}]."""
    entries = []
    for m in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)\n\}", text or "", re.S):
        body = m.group(2)
        get = lambda name: (re.search(name + r"\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", body + "\n", re.I | re.S) or [None, None])[1]
        entries.append({"key": m.group(1), "doi": pp_doi(get("doi")), "title": (get("title") or "").strip("{} "),
                        "raw": m.group(0)})
    return entries


# ---------- two-way project sync ----------

def pp_sync_project(project, local_bib="", session_dir=None):
    """Two-way sync between the project's sources.bib text and its Paperpile folder.
    1) local entries not yet synced are filed into the folder (no duplicates);
    2) entries removed from the folder in Paperpile since the last sync are dropped locally;
    3) returns the folder's full contents as the new sources.bib text (Paperpile is the record).
    Returns status confirm (ask about folder), or ok with bib and a report."""
    d = pp_session_dir(session_dir)
    pf = pp_project_folder(project, create=True, session_dir=d)
    if pf["status"] not in ("ok", "created"):
        return pf
    fid = pf["folder_id"]
    os.makedirs(os.path.join(d, "sync"), exist_ok=True)
    state_name = os.path.join("sync", (pp_norm(project) or "project") + ".json")
    state = pp_read_json(d, state_name, {}) or {}
    synced = set(state.get("keys", []))
    names = state.get("citekeys", {})
    local = pp_bib_parse(local_bib)
    before_keys = set().union(*[pp_ref_keys(r) for r in pp_folder_refs(fid, d)] or [set()])
    removed_upstream = sorted(k for k in synced if k not in before_keys)
    push = [e for e in local if not (pp_ref_keys(e) & before_keys) and not (pp_ref_keys(e) & synced)]
    rep = pp_file(fid, [{"doi": e["doi"], "title": e["title"], "bibtex": e["raw"]} for e in push], d) if push else \
        {"already_in_folder": [], "added_to_folder": [], "saved_new": [], "failed": [], "enriched": []}
    after = pp_folder_refs(fid, d)
    keys_after = set().union(*[pp_ref_keys(r) for r in after] or [set()])
    local_keys = set().union(*[pp_ref_keys(e) for e in local] or [set()])
    gone = set(removed_upstream)
    keep_local = [e["raw"] for e in local
                  if not (pp_ref_keys(e) & keys_after) and not (pp_ref_keys(e) & gone)]
    bib = pp_bibtex(sorted(after, key=lambda r: (r.get("citekey") or "").lower()))
    if keep_local:
        bib += "\n% Not yet in Paperpile (filing failed):\n" + "\n\n".join(keep_local) + "\n"
    pp_write_json(d, state_name, {"project": project, "folder_id": fid, "keys": sorted(keys_after),
                                  "citekeys": {k: r.get("citekey") for r in after for k in pp_ref_keys(r)},
                                  "synced_at": time.time()}, private=False)
    return {"status": "ok", "folder": pf["name"], "folder_id": fid, "folder_status": pf["status"],
            "pushed": rep, "pulled_new": sorted(r.get("citekey") for r in after if not (pp_ref_keys(r) & local_keys)),
            "removed_upstream": sorted({names.get(k, k) for k in removed_upstream}), "count": len(after), "bib": bib}
