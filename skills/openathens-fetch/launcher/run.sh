#!/bin/bash
# Shared entry point of the launchers: make sure the venv exists, run oa_login.py, and
# close the Terminal window on success. Runs outside the Claude Science sandbox; output
# is appended to launcher.log.
cd "$(dirname "$0")" || exit 1
main() {
  if ! ./venv/bin/python -c "import playwright.sync_api" 2>/dev/null; then
    echo "First run: building venv with Playwright (about a minute)..."
    PY=/usr/bin/python3; [ -x "$PY" ] || PY=python3
    "$PY" -m venv venv && ./venv/bin/python -m pip install -q --upgrade pip playwright || return 1
  fi
  if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -f .chromium-installed ]; then
    echo "Google Chrome not found: installing Playwright's Chromium..."
    ./venv/bin/python -m playwright install chromium && touch .chromium-installed
  fi
  ./venv/bin/python -u ./oa_login.py "$@"
}
main "$@" 2>&1 | tee -a launcher.log
rc=${PIPESTATUS[0]}
[ "$rc" -eq 0 ] && ./close-window.sh
exit "$rc"
