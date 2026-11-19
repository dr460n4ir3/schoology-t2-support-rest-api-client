#!/bin/bash
# Optional: sets up the free local AI (Ollama) used by the Assistant tab. macOS and Linux.
#
#   ./install_local_ai.sh              # picks llama3.1:8b (Apple Silicon / Linux, 16 GB+) or qwen2.5:3b
#   ./install_local_ai.sh qwen3:8b     # or choose the chat model yourself
#
# Mac users can double-click "Install Local AI (optional).command" instead.
# Nothing you ask the assistant is sent to an outside AI service.
set -euo pipefail
cd "$(dirname "$0")"

OLLAMA_URL="http://localhost:11434"
MAC_CLI="/Applications/Ollama.app/Contents/Resources"
have() { command -v "$1" >/dev/null 2>&1; }
running() { curl -fsS --max-time 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; }

echo "Optional: installs Ollama + a chat model (about 2-5 GB) + nomic-embed-text (about 270 MB)."
echo

# ---------------------------------------------------------------- install Ollama
[ -x "$MAC_CLI/ollama" ] && export PATH="$MAC_CLI:$PATH"
if ! have ollama; then
    case "$(uname -s)" in
        Darwin)
            echo "Downloading Ollama for Mac..."
            tmp="$(mktemp -d)"
            curl -fL --progress-bar -o "$tmp/Ollama.zip" "https://ollama.com/download/Ollama-darwin.zip"
            ditto -x -k "$tmp/Ollama.zip" /Applications
            rm -rf "$tmp"
            export PATH="$MAC_CLI:$PATH"
            ;;
        Linux)
            echo "Installing Ollama with the official script (may ask for your password)..."
            curl -fsSL https://ollama.com/install.sh | sh
            ;;
        *)
            echo "Unsupported system. Install Ollama from https://ollama.com/download"; exit 1 ;;
    esac
fi
have ollama || { echo "Ollama did not install. Get it from https://ollama.com/download and run this again."; exit 1; }

# ---------------------------------------------------------------- start it
if ! running; then
    echo "Starting Ollama..."
    if [ "$(uname -s)" = "Darwin" ] && [ -d /Applications/Ollama.app ]; then
        open -a Ollama
    else
        nohup ollama serve >/dev/null 2>&1 &
    fi
    for _ in $(seq 1 30); do running && break; sleep 1; done
fi
running || { echo "Ollama isn't responding. Start the Ollama app, then run this again."; exit 1; }

# ---------------------------------------------------------------- models
model="${1:-}"
if [ -z "$model" ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
    else
        ram_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1048576 ))
    fi
    # Intel Macs can't use their graphics chip for AI, so an 8B model takes minutes per answer there.
    # qwen2.5:3b answers our tests just as well and is about 3x faster.
    if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" != "arm64" ]; then
        model="qwen2.5:3b"
        echo "This is an Intel Mac, so the chat model will be ${model} (fast enough without an Apple chip)."
    else
        if [ "$ram_gb" -ge 15 ]; then model="llama3.1:8b"; else model="qwen2.5:3b"; fi
        echo "This computer has ${ram_gb} GB of RAM, so the chat model will be ${model}."
    fi
fi

echo; echo "Downloading ${model}..."
ollama pull "$model"
echo; echo "Downloading nomic-embed-text..."
ollama pull nomic-embed-text

# Tell the API Client which chat model to use.
if [ -x .venv/bin/python ]; then
    RC_MODEL="$model" .venv/bin/python -c "import os; from restclient.config import load_settings, save_settings; s = load_settings(); s.ollama_chat_model = os.environ['RC_MODEL']; save_settings(s)"
fi

echo
echo "Done! Open the API Client and go to the Assistant tab."
echo "(If the app was already open, click the refresh button in the Local AI box.)"
