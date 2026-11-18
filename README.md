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
   Edit it on the **Environments** tab: paste the API keys and fill in IDs.
3. Click a request on the left, then press **Send** (or **Ctrl+Enter** / **⌘Enter**). **Ctrl+S** / **⌘S** saves.
   - `{{user_id}}` is a *variable*. An orange warning tells you which ones still need a value.
   - **All pages** fetches every page of a OneRoster list.
   - The **⋮** menu has *Preview* (see exactly what will be sent) and *Copy as cURL*.
4. **Bulk Runner** tab: pick the request(s), drop in a CSV whose column names match the variables
   (e.g. a `user_id` column), click **Preview first row**, then **Run**. Results are saved
   automatically. Click any row to see the full response.
   - Runs that change data (PUT/POST/DELETE) ask for confirmation first. Try 1–2 rows first!
5. Close the browser tab when you're done. The ⏻ button (top right) fully stops the app.

Your files are in `C:\Users\<you>\SchoologyAPIClient` on Windows or `/Users/<you>/SchoologyAPIClient` on
a Mac (collections, environments, results, logs).

---

## For the admin

### Requirements
Python **3.10+** on Windows or macOS (developed on 3.13). Everything else installs into `.venv` automatically.
On a Mac, install Python from python.org (or Homebrew): the built-in `/usr/bin/python3` is often 3.9, which is
too old. The launcher looks for 3.10+ and says so if it can't find it.

### Install / update on a team PC (Python route)
```bat
git clone https://github.com/dr460n4ir3/schoology-t2-support-rest-api-client.git
```
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

### Sharing collections across the team
**Settings (⚙) → Collections folder**: point everyone at the same shared folder (OneDrive/SharePoint/network
drive) so the team uses one set of collections. Use the ↻ button in the sidebar to pick up teammates' changes.
Keep **Environments** local, because they hold each person's credentials. Deleted items are copied to a
`.trash` folder first.

You can also set `RESTCLIENT_HOME` to move the whole data folder.

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
`send_http_request` (ad-hoc, off by default). It reads the same collections/environments as the app.
The **AI & Help** tab shows ready-to-copy config with the right paths for that PC.

- **GitHub Copilot (VS Code, Agent mode)**: this repo includes `.vscode/mcp.json`. Open the folder in
  VS Code, start the `schoology-api-client` server (Mac: `schoology-api-client-mac`) from the MCP view, and
  use Agent mode.
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

### Settings (`~/SchoologyAPIClient/settings.json`)
`collections_dir`, `environments_dir`, `results_dir`, `timeout_seconds`, `verify_ssl`,
`use_system_certificates` (uses the Windows certificate store / macOS Keychain via `truststore`; needed
behind corporate SSL inspection), `port` (default 8765), plus the MCP switches above.

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
  postman.py              Postman import/export
  ui.py                   NiceGUI interface
  mcp_server.py           AI tools (MCP)
  cli.py                  command line
examples/                 starter collections, example environments, sample CSVs
tests/test_core.py        tests against a local mock API (OAuth1/2, paging, runner, Postman)
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
  **Auth → Get access token (debug)** to see the token server's raw response. Starter collections are only
  copied on first run, so an existing install gets new starter requests by importing
  `examples/collections/OneRoster 1.x.json` (sidebar Import button).
- Variable precedence: collection variables < environment variables < CSV columns / values passed in.
  Blank collection/environment values count as "not set", so the app warns instead of calling `/users/`.
