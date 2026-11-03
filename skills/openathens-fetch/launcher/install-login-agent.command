#!/bin/bash
# Double-click once to let the agent open the login window by itself: installs a per-user
# LaunchAgent that asks Terminal to open oa-login-request.command whenever the agent
# writes login-request. launchd itself only touches the boot volume (plist and log under
# ~/Library): background jobs cannot run programs stored on other volumes (exit 78).
cd "$(dirname "$0")" || exit 1
LABEL=org.oa-session.login
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s#__DIR__#$(pwd)#g" -e "s#__LOG__#$HOME/Library/Logs/$LABEL.log#g" login-agent.plist.in > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
  touch .agent-installed
  echo "LaunchAgent $LABEL installed." | tee -a launcher.log
fi
