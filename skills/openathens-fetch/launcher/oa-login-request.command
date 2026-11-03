#!/bin/bash
# Opened in Terminal by the LaunchAgent when login-request changes. launchd also fires
# when the request file is removed; then there is nothing to do and the window closes.
cd "$(dirname "$0")" || exit 1
[ -f login-request ] || { ./close-window.sh; exit 0; }
exec ./run.sh --on-request
