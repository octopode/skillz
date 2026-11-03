# openathens-fetch

A Claude Science skill that downloads paywalled journal PDFs through **your own** library
OpenAthens login. When the login has expired, a browser window opens on your Mac, you
sign in with your institution's credentials as usual, and the agent reuses that session
until it expires again. It only reaches articles your library already gives you.

macOS only. It defaults to Harvard Library and works with any OpenAthens institution.

## How it works
- A Chrome window (its own profile, kept between logins) runs **outside** the Claude
  Science sandbox, as you. After you log in, it saves only the identity cookies:
  OpenAthens plus your institution's login site. These go to `cookies.json` (readable by you only).
- The agent replays them through the publisher's SAML login chain to download the PDF.
- When the OpenAthens session lapses, the agent re-authenticates silently through your
  institution's login session (no window, no MFA) for as long as that session lasts.
  Only when both have expired does a login window open again.

## Requirements
- Claude Science on a Mac. Google Chrome is recommended; without it, Playwright's
  Chromium is downloaded on first use.
- `/usr/bin/python3`, which comes with the Xcode Command Line Tools
  (`xcode-select --install`).
- A library that uses OpenAthens, and an article page your library subscribes to (the
  "probe").

## Setup
1. Install the skill: Claude Science > Settings > Skills > Import from GitHub, using this
   repo.
2. Create the session folder: `mkdir -m 700 ~/oa-session`. Grant it to Claude Science
   with read/write access when the agent asks (or ahead of time).
3. Ask the agent to "set up openathens-fetch". It runs `oa_install()`.
   - Not at Harvard? Give it your redirector, in the form
     `https://go.openathens.net/redirector/<your-institution-domain>?url=`, and an article
     URL your library subscribes to.
4. In Finder, double-click `oa-login.command` in `~/oa-session` and log in. The first run
   spends about a minute building its Python environment. The window closes itself when
   done.
5. Optional: double-click `install-login-agent.command` once. After that, the agent can
   open the login window by itself whenever a login is needed.
6. Ask for a paper by DOI. The first fetch from each new publisher asks you to allow one
   to three of its login hosts in Claude Science; your institution's login host also needs
   allowing for silent re-login.

## Files in the session folder
| File | What it is |
|---|---|
| `cookies.json` | the saved login (a live credential; never share it) |
| `profile/` | the login browser's profile |
| `config.json` | redirector, probe article, learned login hosts |
| `oa-login.command` | double-click to log in |
| `install-` / `uninstall-login-agent.command` | turn the auto-popup login on or off |
| `launcher.log`, `last_login.json`, `close-window.log` | logs |

## Removing it
Double-click `uninstall-login-agent.command`, then delete `~/oa-session`.

## Limits
- Some publishers (e.g. Wiley, ACS) block scripted downloads even when you're logged in.
  For those, the agent asks you for the PDF.
- If you keep the session folder elsewhere, tell the agent; it sets `OA_SESSION_DIR`.
