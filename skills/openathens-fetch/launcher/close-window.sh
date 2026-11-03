#!/bin/bash
# Close the Terminal window this was started from, about 1 s after its script exits.
# The closer runs detached (new session, no controlling tty), so Terminal does not ask
# to terminate a running process. Windows are addressed by id (closing shifts indices);
# only single-tab, no-longer-busy windows on this tty are closed. Result -> close-window.log
TTY=$(tty 2>/dev/null) || exit 0
cd "$(dirname "$0")" || exit 0
PY=./venv/bin/python; [ -x "$PY" ] || PY=/usr/bin/python3
"$PY" - "$TTY" <<'EOF'
import subprocess, sys, time
script = """delay 1
set closed to 0
tell application "Terminal"
  repeat with wid in (get id of every window)
    try
      set w to window id wid
      if (count of tabs of w) is 1 then
        set t to tab 1 of w
        if tty of t is "%s" and busy of t is false then
          close w
          set closed to closed + 1
        end if
      end if
    end try
  end repeat
end tell
return "%s tty=%s closed=" & closed""" % (sys.argv[1], time.strftime("%H:%M:%S"), sys.argv[1])
log = open("close-window.log", "a")
subprocess.Popen(["/usr/bin/osascript", "-e", script], start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=log, stderr=log)
EOF
