#!/bin/bash
# ---------------------------------------------------------------------------
#  Admin only (Mac): builds a stand-alone app (no Python needed on team Macs).
#  Output: dist/Schoology API Client.app -> zip it and share it.
#  The app is unsigned: the first time, team members right-click > Open > Open.
#  Build on the oldest macOS / the CPU type (Apple silicon vs Intel) your team uses.
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1
fail() { echo; echo "Build failed - see the messages above."; read -r -p "Press Return to close..." _; exit 1; }

[ -x ".venv/bin/python" ] || { echo 'Run "Launch API Client.command" once first to create the Python environment.'; exit 1; }
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || fail
# nicegui-pack calls "pyinstaller" by name, so put the venv's tools first on PATH
export PATH="$(pwd)/.venv/bin:$PATH"
.venv/bin/nicegui-pack --onedir --windowed --noconfirm --clean \
    --name "Schoology API Client" \
    --add-data "examples:examples" \
    run_app.py || fail
echo
echo 'Done. Zip and share:  dist/Schoology API Client.app'
