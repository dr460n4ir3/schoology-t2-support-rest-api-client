# Schoology T2 Support REST API Client

A lightweight Postman replacement for the PS SYG Support team. Written in Python with
[`requests`](https://requests.readthedocs.io/) and a browser-based UI ([NiceGUI](https://nicegui.io)).

- **Collections**: as many as you like, with folders. Ships with starter collections for
  **Schoology API v1**, **OneRoster 1.1** and **OneRoster 1.2**. Imports and exports Postman collections.
- **Environments**: one per district / SIS connection (base URLs, IDs, credentials). Secret values are masked.
- **Auth**: OAuth 1.0 (HMAC-SHA1 / HMAC-SHA256 / PLAINTEXT), OAuth 2.0 client credentials (tokens fetched,
  cached and refreshed automatically), Bearer, Basic, API key. An environment can **override** the
  collection's auth, so one OneRoster collection works for PowerSchool SIS (OAuth 2) *and* Infinite Campus
  1.1 (OAuth 1 HMAC-SHA256).
- **All pages**: pulls every page of a OneRoster list (`limit`/`offset`, `Link: next`, `X-Total-Count`).
- **Bulk Runner**: run one or more requests once per CSV row, with expected-status checks, values saved from
  responses (chained into the next request), 429 retry and an Excel-friendly results CSV.
- **AI assistants**: an MCP server for GitHub Copilot, Open WebUI via **mcpo**, Claude Desktop, etc.
  It is read-only (GET) by default.
- **Windows and Mac, no terminal needed**: double-click `Launch API Client.bat` (Windows) or
  `Launch API Client.command` (Mac), or hand out a stand-alone build (`.exe` / `.app`).

---

## For the team (no coding needed)

1. Double-click **`Launch API Client.bat`** on Windows, or **`Launch API Client.command`** on a Mac
   (or the desktop shortcut). The first start takes a minute while it sets itself up. The app opens in
   your web browser.
   - Mac, first time only: if macOS says *"Apple could not verify…"*, click **Done**, then open
     **System Settings → Privacy & Security**, scroll down to *"Launch API Client.command was blocked"*
     and click **Open Anyway** (enter your Mac password), then **Open Anyway** again. You only do this
     once. A Terminal window appears; you can close it once the browser opens.
2. **Pick an environment** at the top right (the district / SIS you are working on).
   Edit it on the **Environments** tab: paste the API keys and fill in IDs. Click **Test connection** to check
   the keys work (the signal icon above the list tests every environment at once). Keys and secrets are kept in
   the Mac Keychain / Windows Credential Manager, not in the environment files.
3. Click a request on the left, then press **Send** (or **Ctrl+Enter** / **⌘Enter**). **Ctrl+S** / **⌘S** saves.
   - `{{user_id}}` is a *variable*. An orange warning tells you which ones still need a value.
   - **All pages** fetches every page of a OneRoster list.
   - The **⋮** menu has *Preview* (see exactly what will be sent) and *Copy as cURL* (keys hidden).
   - If a call fails, a 💡 line under the response says what the status code means and what to try.
   - **Copy for ticket** (on the response) copies the request, environment, status, meaning and key fields,
     with keys and secrets hidden, ready to paste into a ticket.
   - **Recent** (next to New) lists your last 10 lookups with the ids used; click one to run it again.
     Recent lookups are saved only on your computer (in your app folder), never in the repo.
4. **Bulk Runner** tab: pick the request(s), drop in a CSV whose column names match the variables
   (e.g. a `user_id` column), click **Preview first row**, then **Run**. Results are saved
   automatically. Click any row to see the full response.
   - Runs that change data (PUT/POST/DELETE) ask for confirmation first. Try 1–2 rows first!
5. **Assistant** tab (optional, free, runs on your computer): ask questions in plain English, e.g.
   *"Look up Schoology user 12345 and list their sections"* or *"What do our docs say about SYG-ROSTER-409?"*.
   It searches documents you add (PDF/Word/text) and calls the API with your saved requests.
   First time: double-click **`Install Local AI (optional).bat`** (Mac: **`.command`**). It downloads
   about 2–5 GB, so run it on a good connection. Answers appear as they are written.
   - To ask about a **database query result**: export it as CSV (Sequel Ace: **File → Export → CSV**), click
     the 📎 next to the chat box and pick the file. It's read into memory only and never leaves your computer.
     The app counts blanks and values for the AI, so "how many…" questions get exact numbers. Small models do
     best with one question at a time.
6. **Database** tab (optional): one-click sign-in and a list of your database connection files.
   Under **Setup**, type the commands you normally run in Terminal to sign in (one per line), the folder
   they run in, and the folder where the sign-in puts one connection file per database. Then click
   **Sign in** (a browser window may open to sign in) and click a database to open it in your
   database app (e.g. Sequel Ace). These settings stay on your computer.
7. **Updates**: when a new version is on GitHub, a green **Update available** button appears at the top.
   Click it, then **Update now** and **Restart now**. You can also check under **Settings (⚙) → Check for
   updates**.
8. **Team** (the 👥 button above the request list): share request changes with your teammates.
   - Add, edit, rename or delete requests as usual and click **Save**. A blue number on 👥 counts your
     changes that teammates don't have yet. An orange number means teammates shared something new.
   - Click 👥, check the list, type a short note about what you changed and click **Share my changes**.
     Click **Get team changes** to pick up theirs. The ↶ next to a change undoes it.
   - If you and a teammate changed the same request, the app asks which version to keep.
   - The first time, it asks for your name and email (the one on your GitHub account) to label your changes.
   - Never put real keys in a request. Use `{{variables}}` and keep the keys in your environment. The app
     refuses to share a request that contains a real key or secret.
9. Close the browser tab when you're done. The ⏻ button (top right) fully stops the app.

Requests live in the project folder's `collections` folder, one file per request (see *Team collections*
below). Everything personal is in `C:\Users\<you>\SchoologyAPIClient` on Windows or
`/Users/<you>/SchoologyAPIClient` on a Mac: environments, results, recent lookups, logs and the trash.

---

## For the admin

### Requirements
Python **3.10+** on Windows or macOS (developed on 3.13). Everything else installs into `.venv` automatically.
On a Mac, install Python from python.org (or Homebrew): the built-in `/usr/bin/python3` is often 3.9, which is
too old. The launcher looks for 3.10+ and says so if it can't find it.

### Install / update on a team PC (Python route)
```bat
git clone https://github.com/<owner>/<repo>.git
```
(Use the address from the repo's green **Code** button on GitHub, or clone it with GitHub Desktop.)
Then double-click `Launch API Client.bat` (Mac: `Launch API Client.command`), and optionally
`Create Desktop Shortcut.bat` (Mac: `Create Desktop Shortcut.command`). The `.command` files are committed
as executable; if a Mac says they aren't, run `chmod +x *.command` once in the repo folder.

**macOS "Apple could not verify…" warning**: macOS blocks unsigned scripts/apps that were *downloaded*
(browser, ZIP, Teams, email). Ways around it, best first:
- Install with `git clone` (or GitHub Desktop) instead of downloading a ZIP. Cloned files aren't flagged,
  so no warning appears.
- Clear the download flag once on that Mac: `xattr -dr com.apple.quarantine "/path/to/the/folder"`.
- Or have each person allow it once: System Settings → Privacy & Security → **Open Anyway**.
- To remove the warning for everyone permanently, the build has to be signed and notarized with an
  Apple Developer ID (ask whether your company already has one), or pushed out through your Mac
  management tool (Self Service / Jamf / Intune).
After a `git pull`, the launcher re-installs components only if `requirements.txt` changed.

### Stand-alone build (no Python on team PCs)
- **Windows**: double-click `build_exe.bat`. It produces `dist\Schoology API Client\`. Zip that folder and
  share it; people unzip and run `Schoology API Client.exe`.
- **Mac**: double-click `build_app.command` on a Mac (PyInstaller can't build for another OS). It produces
  `dist/Schoology API Client.app`. Zip and share it. The app isn't code-signed, so the first launch needs
  **System Settings → Privacy & Security → Open Anyway** (see above). An Apple silicon build only runs on
  Apple silicon Macs; build on Intel for Intel.

Rebuild and re-share when you change the code.
(The AI/MCP connection needs the Python route.)

**Built by GitHub (recommended)**: `.github/workflows/build.yml` builds all three versions on GitHub's
machines: Windows, Intel Mac and Apple-chip Mac. To publish a version:
1. Bump `__version__` in `restclient/__init__.py` and commit.
2. Tag it and push the tag: `git tag v1.1.0` then `git push origin v1.1.0`.
3. In a few minutes the zips appear under the repo's **Releases**.

Team members download the zip for their computer, unzip it and open the app. The stand-alone app's
**Check for updates** compares its version with the latest release and links to the download. Every push
to `main` also runs the tests there. **Actions → Build → Run workflow** builds without publishing (the zips
appear under that run's *Artifacts*).

### Team collections (one file per request, shared through GitHub)
When the app runs from a `git clone`, it uses the repo's **`collections/`** folder, laid out like the app's
tree:
```
collections/
  Schoology API/                  one folder per collection
    _collection.json              collection name, description, auth, variables, order of items
    Users/                        one folder per folder
      _folder.json                folder name, description, order of items
      Get user.json               one file per request
```
A request file is short and can be edited by hand, in the app or on GitHub:
```json
{
  "name": "Get user",
  "method": "GET",
  "url": "{{base_url}}/users/{{user_id}}",
  "description": "Schoology user id (not the SIS id).",
  "params": [{"key": "extended", "value": "true"}],
  "id": "e7a33968ded2"
}
```
Only `name`, `method` and `url` are required. Optional fields: `description`, `params`, `headers`,
`body_mode` (`json`, `text`, `urlencoded`, `form`), `body`, `form`, `auth` (default: use the collection's),
`fetch_all_pages`, and `"enabled": false` on a param or header. A new file without an `id` or an `"order"`
entry still shows up (after the listed items, A-Z). A deleted file just disappears. If a file has a typo, the
app skips that one file and says which one.

**Working together**:
- In the app: the **Team** (👥) button shows your changes, **Share my changes** commits and pushes only
  `collections/`, and **Get team changes** pulls. If two people changed the same request, it asks whose
  version to keep. It checks GitHub every 10 minutes. App updates still come through **Update available**,
  and your unshared request edits never block them.
- In git directly: edit, `git add collections`, `git commit`, `git pull --rebase`, `git push`. Pull requests
  work too, if you'd rather review changes first.

**Setting up a teammate**:
1. On GitHub: repo **Settings → Collaborators → Add people**, then add their GitHub account (they accept the
   email invite). They need this to share changes.
2. They install with `git clone` (or GitHub Desktop → *Clone repository*), not a ZIP download.
3. The first share asks GitHub to sign them in. If the app says GitHub needs a sign-in, they open a terminal
   in the app folder, type `git push`, and sign in once in the window that appears (or sign in to GitHub
   Desktop). Then they click **Share** again.

**If the repo is public**, anyone can read the request files. They never contain keys: the app blocks
sharing a request with a real key, token or password in it. Still, don't put district names, student ids or
notes about real people in requests or descriptions. Ids belong in environments and CSVs (which stay on
each computer). If that's too limiting, make the repository private (repo Settings → General → Danger
Zone → *Change visibility*). Everything keeps working. The stand-alone build's update check would then
need a different place to download from.

Deleted requests and collections are copied to `SchoologyAPIClient/trash` first. The stand-alone build
(no git) keeps collections in `SchoologyAPIClient/collections` instead. To share those, point
**Settings (⚙) → Collections folder** at a shared drive.

**Earlier installs**: before this version, collections lived in `SchoologyAPIClient/collections` as one
file each. A git copy now uses the repo's `collections/` folder, and the old files are left where they are.
To bring across a request you made yourself, use **Import** on the old file. Old one-file collections in a
collections folder are converted to the new layout automatically.

### Command line
On a Mac use `.venv/bin/python` instead of `.venv\Scripts\python`.
```bat
.venv\Scripts\python -m restclient.cli list
.venv\Scripts\python -m restclient.cli send "Schoology API" "Users/Get user" --env "Schoology - Example District" --var user_id=12345
.venv\Scripts\python -m restclient.cli send "OneRoster 1.1" "All users" --env "PowerSchool SIS - OneRoster 1.1 (example)" --all-pages > users.json
.venv\Scripts\python -m restclient.cli run "Schoology API" "Update user" --csv emails.csv --env "..." --expect 200 --delay 250
```

### Connecting AI (MCP)
The MCP server (`restclient/mcp_server.py`) exposes these tools: `list_collections`, `list_requests`,
`list_environments`, `preview_request`, `send_saved_request`, `run_bulk`, `create_request`, and
`send_http_request` (ad-hoc, off by default), and `search_knowledge_base` (needs the local AI). It reads the same collections/environments as the app.
The **AI & Help** tab shows ready-to-copy config with the right paths for that PC.

- **GitHub Copilot (VS Code, Agent mode)**: on the **AI & Help** tab, click **Set up Copilot for me**, then
  quit and reopen VS Code. That adds the server to the "Copilot global" file `~/.copilot/mcp-config.json`
  (Windows: `%USERPROFILE%\.copilot\mcp-config.json`), keeping any other servers in it and backing up the old
  file. To do it by hand: in VS Code **File → New Text File**, paste the box from the AI & Help tab (paths
  already filled in), and save it at that path (Mac Save box: **⌘ Shift G**, `~/.copilot`). Then open Copilot
  Chat, switch to **Agent** mode and ask *"Which collections and environments do I have?"*.
  ```json
  {"mcpServers": {"schoology-api-client": {"type": "local", "command": "/path/to/repo/.venv/bin/python",
    "args": ["-m", "restclient.mcp_server"], "env": {"PYTHONPATH": "/path/to/repo"}, "tools": ["*"]}}}
  ```
  Older VS Code versions don't have the Copilot global file: use **⌘/Ctrl Shift P** →
  `>MCP: Open User Configuration` and the "Older VS Code" box on the AI & Help tab (`"servers"` format).
  This repo also includes `.vscode/mcp.json` for when the repo folder itself is open in VS Code.
- **mcpo → Open WebUI (or any chatbot that supports OpenAPI tools)**:
  ```bat
  uvx mcpo --port 8000 --api-key "CHANGE-ME" -- "C:\path\to\repo\.venv\Scripts\python.exe" -m restclient.mcp_server
  ```
  On a Mac the python path is `/path/to/repo/.venv/bin/python`.
  Then add `http://localhost:8000` (with that API key) as a Tool Server in Open WebUI. mcpo runs in its own
  environment (`uvx`/`pipx`), so it doesn't need to share this app's `.venv`.
- **Claude Desktop / Cursor / others**: command = the `.venv` python, args = `-m restclient.mcp_server`.

**Safety defaults** (AI & Help tab, or `settings.json`):
| Setting | Default | Meaning |
|---|---|---|
| `mcp_allow_write_methods` | `false` | AI may only send GET/HEAD/OPTIONS |
| `mcp_allow_adhoc_requests` | `false` | AI may only use requests saved in a collection |
| `mcp_max_response_chars` | `20000` | Response text returned to the AI is truncated |

Secret environment values are never returned to the AI. Note that when the AI sends a request, the API
response itself (e.g. student records) goes to the AI service your company uses, so follow your data policy.

### Local AI assistant (Ollama, optional)
The **Assistant** tab is a chat that runs on [Ollama](https://ollama.com) on the same computer, so questions,
documents and API responses never leave the PC. It is the "RAG + API calls in one tool" option:
- **Knowledge base**: upload PDF / Word / text / Markdown / HTML. The files are chunked and embedded with
  `nomic-embed-text`, then stored in `~/SchoologyAPIClient/knowledge` (`knowledge_dir` can point at a shared
  drive so the team shares one knowledge base). The best matches are given to the model for every question,
  and the answer cites them.
- **API calls**: the model gets the MCP tools (`list_requests`, `send_saved_request`, `run_bulk`,
  `search_knowledge_base`, ...) with the same safety switches. `create_request` is not offered.
- **Install**: `Install Local AI (optional).bat` (Windows: winget, or the official installer as a fallback),
  `Install Local AI (optional).command` (Mac), or `./install_local_ai.sh [model]` (Mac/Linux, bash).
  They install Ollama, pick a chat model, pull `nomic-embed-text` and save the choice in settings:
  `llama3.1:8b` on Apple-chip Macs / Linux with 16 GB+ or Windows PCs with an NVIDIA card, otherwise
  `qwen2.5:3b` (Intel Macs and laptops without a graphics card run models on the CPU, where an 8B model
  takes minutes per answer). Models can also be switched and downloaded from the Assistant tab, and
  Ollama can be started/stopped there (Stop frees its memory).
- **Model choice**: on our test set (user lookups, follow-ups, a runbook question, a missing user, app
  how-tos, "what do I have") `qwen2.5:3b` and `llama3.1:8b` both answer 7/7; `llama3.2:3b` invents API data,
  so don't use it. What makes small models reliable: the app only offers API tools when the question is a
  real lookup (`needs_tools`), lists only the saved requests that fit the selected environment, and fixes
  mixed-up request names before calling the API. A short app guide in the prompt answers how-to questions.
- No LangChain or vector database is needed: plain HTTP to Ollama plus a dot-product search
  (`restclient/ollama.py`, `knowledge.py`, `assistant.py`). The tools also appear in MCP as
  `search_knowledge_base`, so Copilot can use the knowledge base too.

### Database tab (sign-in launcher)
A generic launcher for "run our sign-in scripts, then open a connection file" workflows; the app has no
knowledge of any company tool, and nothing about one belongs in this repo.
- `db_signin_commands` run one at a time in `db_tool_folder` (added to `PATH`, with `bin/`), in the
  user's login shell (`$SHELL -l -i -c`, so aliases and `~/.zshrc` PATH work), with this app's own Python
  removed from the environment. The run stops at the first failing command. A command may leave a background
  process running (e.g. a tunnel); the next step still starts. Commands that ask for typed input: turn on
  `db_use_terminal` to run them in a Terminal window instead.
- `db_connections_dir` is listed (subfolders become groups; bundles such as `.spfs` count as one item).
  Clicking one opens it like a double-click, or with `db_open_with` on a Mac (e.g. `Sequel Ace`).
  The app never reads the connection files, so it never sees database passwords.
- Output is kept in memory only (not logged), since sign-in tools can print sensitive details.

### Keys, secrets and privacy
- **Keychain**: values marked secret (🔒 on the Environments tab) and auth secrets (consumer/client secret,
  password, token) are stored in the Mac Keychain (entries named *Schoology API Client* in Keychain Access) or
  Windows Credential Manager. The environment file only says `"keychain": true`. Older files with plain-text
  secrets are moved automatically the first time the app loads them. Turn it off with `use_keychain` in
  Settings (the secrets then move back into the files). Without a password store (some Linux setups) the app
  keeps using the files.
- **Hidden everywhere text leaves the window**: keys, secrets and tokens are replaced with `[hidden]` in Bulk
  Runner results and their CSV files, copied/downloaded responses, *Copy as cURL*, *Copy for ticket*, the log
  file and everything the AI sees (`restclient/redact.py`). The screen itself still shows the real response.
  *Copy as cURL with keys* is the only exception, for your own use.
- **Recent lookups** are stored in `~/SchoologyAPIClient/recent_lookups.json`. Base URLs and credentials are
  kept as `{{variables}}` there, so the file holds only the ids you looked up. `.gitignore` also lists it,
  so it can't be committed.

### Settings (`~/SchoologyAPIClient/settings.json`)
`collections_dir`, `environments_dir`, `results_dir`, `timeout_seconds`, `verify_ssl`,
`use_system_certificates` (uses the Windows certificate store / macOS Keychain via `truststore`; needed
behind corporate SSL inspection), `port` (default 8765), `use_keychain` (default on), plus the MCP switches, Ollama and Database tab
settings above (`db_*`, blank by default).

### Project layout
```
Launch API Client.bat     Windows double-click launcher (creates .venv on first run)
Launch API Client.command Mac double-click launcher
Create Desktop Shortcut.bat / .command
build_exe.bat             Windows PyInstaller build via nicegui-pack
build_app.command         Mac PyInstaller build (.app)
run_app.py                entry point for the exe build
restclient/
  engine.py               resolve variables + auth, send with requests, redirects, paging, cURL
  runner.py               CSV bulk runner (shared by UI, CLI, MCP)
  models.py               collections / folders / requests / environments (JSON)
  variables.py            {{variable}} substitution and precedence
  storage.py, workspace.py  load/save files
  collection_files.py     collections on disk: a folder per collection, a file per request
  team.py                 Team button: show / share / get / undo collection changes with git
  postman.py              Postman import/export
  launcher.py             Database tab: run sign-in commands, list/open connection files
  secret_store.py         keys/secrets in the Mac Keychain / Windows Credential Manager
  redact.py               hides keys, secrets and tokens in anything copied, saved, logged or sent to the AI
  support.py              error tips, copy for ticket, connection test, recent lookups, data attachments
  updater.py              "Check for updates" (git pull, or GitHub Releases for the stand-alone app)
  copilot_setup.py        writes the Copilot global MCP file (AI & Help tab button)
  ui.py                   NiceGUI interface
  mcp_server.py           AI tools (MCP)
  cli.py                  command line
collections/              the team's shared requests (one file each), edited in the app or by hand
examples/                 example environments, sample CSVs
tests/test_core.py        tests against a local mock API (OAuth1/2, paging, runner, Postman)
tests/test_assistant.py   local AI assistant + knowledge base (fake Ollama)
tests/test_launcher.py    Database tab command runner and connection list
tests/test_support.py     keychain, redaction, tips, ticket text, connection test, recent, updates, streaming
tests/test_team.py        one-file-per-request format, sharing between two clones, clashes, undo
.github/workflows/build.yml  tests on every push; Windows + Mac builds and a Release for each v* tag
```

### Tests
```bat
.venv\Scripts\python -m pytest -q        # Mac: .venv/bin/python -m pytest -q
```

### Notes on the starter content
- **Schoology**: two-legged OAuth 1.0 with the consumer key/secret from `https://<district>.schoology.com/api`.
  Some calls (e.g. `/users/me`) answer with a 303 redirect. The client re-signs redirected requests.
- **OneRoster**: the example environments' URLs follow common PowerSchool / Infinite Campus patterns but
  vary by district and version. **Copy the exact base URL, token URL and keys from the district's SIS**
  connection setup.
- **OneRoster tokens**: every OneRoster request gets its OAuth 2.0 bearer token automatically (collection
  auth: `{{token_url}}`, `{{client_id}}`, `{{client_secret}}`). To troubleshoot a token problem, send
  **Auth → Get access token (debug)** to see the token server's raw response.
- Variable precedence: collection variables < environment variables < CSV columns / values passed in.
  Blank collection/environment values count as "not set", so the app warns instead of calling `/users/`.
