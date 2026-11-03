#!/bin/bash
cd "$(dirname "$0")" || exit 1
LABEL=org.oa-session.login
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/Library/Logs/$LABEL.log" .agent-installed
echo "LaunchAgent $LABEL removed." | tee -a launcher.log
