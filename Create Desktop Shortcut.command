#!/bin/bash
# Mac: puts a "Schoology API Client" shortcut on your Desktop. Double-click once.
cd "$(dirname "$0")" || exit 1
chmod +x "Launch API Client.command"
ln -sf "$(pwd)/Launch API Client.command" "$HOME/Desktop/Schoology API Client.command"
echo "Shortcut created on your Desktop: Schoology API Client"
