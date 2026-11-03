---
name: openathens-fetch
description: "Fetch paywalled journal PDFs the user is entitled to through their library's OpenAthens (SAML) subscription, by replaying their own browser login session through the publisher login chain. A semi-automatic login step is included: when the session has expired, a Chrome window opens on the Mac running Claude Science, the user logs in with their institution's credentials, and the cookies are captured into a private jar reused until it expires. Use when a DOI resolves to a closed-access paper and fetch_article_fulltext returns no full text. macOS only; defaults to Harvard Library, and any OpenAthens institution can be configured."
---

# openathens-fetch

Downloads paywalled PDFs the user can already read, using their own library login.
`kernel.py` (loaded with this skill) provides the helpers below. `launcher/` holds the
login window that runs outside the sandbox. `README.md` is the human setup guide.

| Helper | Purpose |
|---|---|
| `oa_fetch(doi)` | fetch one PDF; returns `{'status': ...}` |
| `oa_session_status()` | `valid` / `login_required` / `needs_allowlist`, without downloading |
| `oa_request_login()` / `oa_wait_for_login(since)` | trigger and await an interactive login |
| `oa_install(...)` | one-time deployment of the launcher into the session folder |
| `oa_jar_info()` | safe jar summary (age, count, domains, `silent_relogin`), never the values |

Prefer `fetch_article_fulltext` first (open-access copies); use this skill for the
closed remainder.

## Session folder
All state lives in one folder that the user creates (`mkdir -m 700 ~/oa-session`) and
grants rw. The helpers find it via `session_dir=`, else `$OA_SESSION_DIR`, else
`~/oa-session`. If memory says the user keeps it elsewhere, set
`os.environ["OA_SESSION_DIR"]` once per kernel.

Contents: `launcher` files, `config.json` (redirector, probe article, learned
`idp_hosts`), `cookies.json` (the jar, mode 0600), `profile/` (dedicated Chrome
profile that keeps the institution login and remembered device), `venv/` (built on
first launch), `last_login.json`, `launcher.log`.

## First-time setup (agent side)
1. Ask the user to create and grant the folder if it is missing.
2. `oa_install()`, passing `redirector=` for institutions other than Harvard, in the form
   `https://go.openathens.net/redirector/<institution-domain>?url=`, and `probe_url=` for any
   article page their library subscribes to (the default is a Nature article).
3. Tell the user to double-click `oa-login.command` in the folder for the first login;
   the first run builds the venv, which takes about a minute. Optionally they double-click
   `install-login-agent.command` once, so later logins pop up by themselves.

`oa_install()` reads the launcher from this skill's cached folder
(`../../skills/openathens-fetch/launcher` relative to the workspace). If that layout
changes, pass `launcher_src=`.

## Fetch workflow
```python
res = oa_fetch("10.1038/nchembio.1733")
print({k: v for k, v in res.items() if k != "chain"})
```
- `ok`: `res["path"]` is a verified PDF; save it with `save_artifacts`.
- `needs_allowlist`: `request_network_access(domain=res["host"])` and retry. A new
  publisher usually asks for one to three hosts (its SP, then its IdP). The
  institution's login host (e.g. `login.harvard.edu`) needs to be allowed for silent
  re-login.
- `login_required`: run the login loop below, then retry.
- `no_pdf_link` / `not_pdf`: authenticated, but no PDF (bot wall such as Wiley or ACS, or
  HTML only). Pass `pdf_url=` if known; otherwise ask the user for the PDF.

Before a batch, call `oa_session_status()` once and log in first if needed.

## Login loop
1. `req = oa_request_login()`
2. `send_user_message(req["tell_user"])` straight away. With the LaunchAgent installed,
   Terminal and a Chrome window open on the Mac by themselves; otherwise the user
   double-clicks `oa-login.command`. If the profile is still logged in, the window
   captures and closes in about 5 s without input.
3. In a BACKGROUND cell: `oa_wait_for_login(req["requested_at"])` returns `captured`,
   `login_failed` or `timeout`.
4. On `captured`, retry the fetch. If `oa_jar_info()["silent_relogin"]` is False, the
   institution's login host was not seen during that login (OpenAthens was still logged
   in). The jar works until OpenAthens expires, and the next full login fixes it.

## How it works
- The jar holds only identity cookies: OpenAthens plus the institution's login hosts
  (`idp_hosts`, learned from the login window's redirects and kept in config.json).
  Publisher cookies are re-issued by the SAML chain on every fetch.
- `oa_fetch` follows the chain: redirector (polls its JS wait page), publisher SP,
  OpenAthens, institution login (only when the OpenAthens session has lapsed; its
  session cookies get a SAML assertion server-side, so no JS and no MFA), SAML POSTs,
  publisher. If the chain stops on a page outside the article's site, a login is needed.
  Refreshed identity cookies are written back after each successful fetch.
- The login window cannot run in the sandbox (Chrome aborts there). `run.sh` runs it as
  the user; the LaunchAgent asks Terminal to open `oa-login-request.command` when
  `login-request` changes. launchd only touches files on the boot volume, because jobs
  whose program or log lives on another volume fail with exit 78 (EX_CONFIG).
- After a successful run, `close-window.sh` closes the Terminal window: a detached
  AppleScript closes single-tab, idle windows on the run's tty, addressed by window id.
  Failed runs stay open so the error can be read. Outcomes are logged in `close-window.log`.

## Security
- Never print cookie values, never save or upload `cookies.json`, `profile/` or
  `config.json`, and never copy them out of the session folder.
- The jar carries the same library access as the user's browser. To revoke, the user
  double-clicks `uninstall-login-agent.command`, then deletes `cookies.json` and `profile/`
  (or the whole folder).

## Maintenance notes
- In Playwright's sync API `page.url` only updates while a Playwright call runs: in
  `oa_login.py`, wait with `page.wait_for_timeout()`, never `time.sleep()`.
- kernel.py sidecar rules: top-level assignments must be literals, no `_`-prefixed
  top-level names.
- Legacy input: `oa_fetch(doi, cookies=oa_cookies_from_header(header))` accepts a
  DevTools Cookie line for login.openathens.net. It has no write-back and no silent
  re-login.
