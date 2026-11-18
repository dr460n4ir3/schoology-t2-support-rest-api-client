#!/bin/bash
# ---------------------------------------------------------------------------
#  Schoology API Client launcher for Mac - double-click me.
#  First run: creates a private Python environment and installs components.
#  After that it just opens the app in your browser. You can close the
#  Terminal window once the browser opens; the app keeps running.
#  (First time only: if macOS blocks it, right-click > Open > Open.)
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1

fail() {
    echo
    echo "  $1"
    echo
    read -r -p "Press Return to close..." _
    exit 1
}

if [ ! -x ".venv/bin/python" ]; then
    echo "Setting up the Schoology API Client for the first time. This takes a minute or two..."
    PY=""
    # Prefer a specific new Python; /usr/bin/python3 on older macOS is too old (3.9)
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cand" >/dev/null 2>&1 &&
           "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PY="$cand"
            break
        fi
    done
    [ -n "$PY" ] || fail "Python 3.10 or newer is required but was not found. Install it from Self Service or python.org, then double-click this file again."
    "$PY" -m venv .venv || fail "Could not create the Python environment (.venv)."
fi

# Re-install components only when requirements.txt has changed, e.g. after an update
if ! cmp -s requirements.txt .venv/installed-requirements.txt; then
    echo "Installing components..."
    .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt ||
        fail "Installing components failed - see the messages above. If you are on the company network, a proxy may be blocking pip. Ask the app admin."
    cp requirements.txt .venv/installed-requirements.txt
fi

# Run in the background so closing this Terminal window doesn't stop the app
nohup .venv/bin/python -m restclient >/dev/null 2>&1 &
disown
echo "Schoology API Client is starting in your browser. You can close this window."
