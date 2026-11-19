#!/bin/bash
# Double-click on a Mac to set up the free local AI for the Assistant tab (runs install_local_ai.sh).
# If macOS says it "could not verify" this file: System Settings > Privacy & Security > Open Anyway.
cd "$(dirname "$0")"
bash ./install_local_ai.sh "$@" || echo "Setup did not finish - see the message above."
echo
read -n 1 -s -r -p "Press any key to close this window..."
echo
