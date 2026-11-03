#!/usr/bin/env python3
"""OpenAthens login capture. Runs OUTSIDE the Claude Science sandbox, as the user.

Opens a Chrome window (persistent profile in this folder) at the library's OpenAthens
redirector for a probe article. The user completes the institutional login; when the
window lands on the publisher, the identity cookies (OpenAthens + the institution's
login site) are written to cookies.json (mode 0600). Cookie values are never printed.

Usage (normally via run.sh):
  oa_login.py               interactive
  oa_login.py --on-request  LaunchAgent mode: only runs if `login-request` exists
Needs config.json (written by the skill's oa_install()). Python >= 3.8.
"""
import json
import os
import sys
import time
from urllib.parse import quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
JAR = os.path.join(HERE, "cookies.json")
STATUS = os.path.join(HERE, "last_login.json")
REQUEST = os.path.join(HERE, "login-request")
LOCK = os.path.join(HERE, ".login.lock")
PROFILE = os.path.join(HERE, "profile")
CHROME_APP = "/Applications/Google Chrome.app"
STALE_LOCK_S = 1500
STABLE_S = 3  # seconds the window must sit on the publisher page


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def write_private(path, obj):
    """Atomic JSON write, mode 0600."""
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def site(host):
    """Crude registrable domain: last two labels (sp.nature.com -> nature.com)."""
    return ".".join(host.split(".")[-2:])


def domain_matches(cookie_domain, host):
    d = cookie_domain.lstrip(".")
    return host == d or host.endswith("." + d)


def is_identity_cookie(cookie, idp_hosts):
    """OpenAthens cookies plus cookies of the institution's login hosts."""
    return cookie["domain"].lstrip(".").endswith("openathens.net") or any(
        domain_matches(cookie["domain"], h) for h in idp_hosts)


def take_lock():
    if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) > STALE_LOCK_S:
        os.remove(LOCK)
    try:
        fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def on_publisher(urls, probe):
    host = urlparse(probe).netloc
    return any(urlparse(u).netloc == host and not any(k in u for k in ("/login", "/saml", "/sso"))
               for u in urls)


def capture(cfg):
    """Drive the login window; return the result dict for last_login.json."""
    from playwright.sync_api import sync_playwright

    probe = cfg["probe_url"]
    timeout_s = int(cfg.get("login_timeout_s", 900))
    nav_hosts = set()
    t0 = time.time()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="chrome" if os.path.exists(CHROME_APP) else None,
            headless=False, viewport=None, args=["--window-size=1100,900"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def note_hop(request):  # every main-frame hop, redirects included
            if request.is_navigation_request() and request.frame == page.main_frame:
                nav_hosts.add(urlparse(request.url).netloc)

        page.on("request", note_hop)
        page.bring_to_front()
        log("opening library login for " + probe)
        try:
            page.goto(cfg["redirector"] + quote(probe, safe=""), timeout=60000)
        except Exception as e:  # the user can still finish the login
            log("initial navigation warning: " + repr(e)[:160])
        log("finish the login in the browser window (waiting up to %d min)" % (timeout_s // 60))
        outcome, stable = "timed out waiting for the login to finish", 0
        while time.time() - t0 < timeout_s:
            # page.wait_for_timeout, NOT time.sleep: the sync API only updates page.url
            # while a Playwright call is running.
            try:
                page.wait_for_timeout(1000)
                urls = [pg.url for pg in ctx.pages if not pg.is_closed()]
            except Exception:
                urls = []
            if not urls:
                outcome = "browser window closed before the login finished"
                break
            stable = stable + 1 if on_publisher(urls, probe) else 0
            if stable < STABLE_S:
                continue
            page.wait_for_timeout(2000)  # let late Set-Cookies land
            probe_site = site(urlparse(probe).netloc)
            learned = {h for h in nav_hosts
                       if h and not h.endswith("openathens.net") and site(h) != probe_site}
            idp_hosts = sorted(set(cfg.get("idp_hosts", [])) | learned)
            cookies = [c for c in ctx.cookies() if is_identity_cookie(c, idp_hosts)]
            ctx.close()
            write_private(JAR, {"captured_at": time.time(), "idp_hosts": idp_hosts,
                                "cookies": cookies})
            if idp_hosts != cfg.get("idp_hosts"):
                cfg["idp_hosts"] = idp_hosts
                write_private(CONFIG, cfg)
            log("captured %d identity cookies (login hosts: %s)"
                % (len(cookies), ", ".join(idp_hosts) or "none seen"))
            result = {"ok": True, "n_cookies": len(cookies), "idp_hosts": idp_hosts,
                      "seconds": round(time.time() - t0)}
            if not idp_hosts:
                # OpenAthens was still logged in, so the institution's login site was never
                # visited and cannot be learned yet. The jar works until OpenAthens expires;
                # the next full login (or idp_hosts in config.json) restores silent re-login.
                result["warning"] = "institution login host unknown: silent re-login disabled"
                log("WARNING: " + result["warning"])
            return result
        try:
            ctx.close()
        except Exception:
            pass
    return {"ok": False, "error": outcome}


def main():
    if "--on-request" in sys.argv and not os.path.exists(REQUEST):
        return 0  # launchd also fires when the request file is removed
    if not take_lock():
        log("a login window is already open; exiting")
        return 0
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with open(CONFIG) as fh:
            result = capture(json.load(fh))
    except Exception:
        import traceback
        result = {"ok": False, "error": traceback.format_exc()[-1500:]}
    finally:
        for path in (LOCK, REQUEST):
            if os.path.exists(path):
                os.remove(path)
    result.update(started=started, finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    write_private(STATUS, result)
    log("done: " + ("OK" if result["ok"] else result["error"]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
