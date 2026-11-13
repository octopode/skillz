# paperpile-mcp: setup guide

Lets Claude Science search, read and file papers in your Paperpile library with your own
Paperpile account. It stands in for a connector: Paperpile doesn't yet accept Claude
Science's connector sign-in. Requires access to Paperpile's MCP/API beta.

## One-time setup
1. Create a private folder for the session and grant it to Claude Science (read/write)
   when asked: `mkdir -m 700 ~/paperpile-session` (or anywhere; tell the agent where).
2. Copy your Paperpile cookie (Chrome; you must be logged in to Paperpile):
   1. Open a new tab, press ⌥⌘I, choose the **Network** tab, tick **Preserve log**.
   2. Go to `https://api.paperpile.com/api/oauth/authorize` (an error page is fine).
   3. Click the `authorize` row, then **Headers**, then **Request Headers**.
   4. Copy everything after `Cookie: ` (one long line including `plack_session=...`).
3. In Claude Science: Customize → Credentials → **Add** → name it `PAPERPILE_SESSION`
   and paste the line as its value. Don't paste it into a chat.
4. Ask the agent to set up Paperpile. It stores the cookie in the session folder and checks
   the connection.

## Day to day
Nothing to do. Paperpile's access tokens last one hour; the skill gets a new one by itself,
using your cookie, in about half a second.

How the agent treats literature:
- **Library first.** It searches your Paperpile library, and reads full texts stored there,
  before looking anywhere on the web.
- **No duplicates.** Papers already in your library are never saved again; they are only
  added to folders.
- **One folder per project.** Each Claude Science project gets a top-level Paperpile folder
  with exactly the project's name (if a similar folder exists, e.g. `EnergyBudget` for
  `energy-budget`, it asks once which to use). Sources the project uses are filed there
  automatically, and the agent tells you what it filed.
- **Two-way sync.** The project also keeps a `sources.bib` artifact. Papers you add to the
  folder in Paperpile appear in it at the next sync; papers you remove from the folder are
  dropped from it. To drop a source, remove it from the folder in Paperpile.
- Anything else that changes your library (removing, deleting, editing notes, other folders)
  is only done after you say yes.

When Paperpile logs you out (signing out in the browser, or the session expiring after
about a year), the agent will say the session needs refreshing. Repeat steps 2 and 3,
replacing the credential's value. In a pinch, the agent can also send you a sign-in link:
sign in, click Allow, and paste the address of the page that fails to load.

## Safety
- The cookie is your full Paperpile login. It lives only in the credential and in
  `cookies.json` (mode 600) in your session folder. The agent never prints it.
- Access is read-write, so the agent can file papers into your library. It describes any
  change and asks first, unless you've just requested that exact change.
- To revoke: ask the agent to log out and forget the cookie, delete the `PAPERPILE_SESSION`
  credential, and sign out of Paperpile in your browser.
