# REST API Client: Team Walkthrough Notes

Notes for walking the team through the app and its code. The full manual is [README.md](README.md).

## What the app is

The app replaces Postman for T2 support. It sends Schoology and OneRoster (1.1 and 1.2) API requests, and it's built for support staff rather than engineers.

- It runs on your own computer and opens in your browser. Nothing is hosted anywhere.
- It works on Windows and Mac.
- It's written in Python. The screen is built with NiceGUI, a Python library that draws a web page.
- Requests are grouped into collections and folders, like Postman. Environments hold the values that change per district: base URL, keys and ids.
- Extras for support work:
  - **Bulk Runner:** runs one request for every row of a CSV.
  - **Copy for ticket.**
  - **Error tips.**
  - **Recent lookups.**
  - **Database tab.**
  - **Optional local AI helper.**

Safety comes first. Keys are hidden on screen and kept in the computer's keychain. They're blanked out of anything copied, saved, logged or sent to the AI.

## Getting it running

Everyone on the team should install with git, because only a git copy has the Team button for sharing requests.

| Way to install | Team button | Updates | Who it's for |
| --- | --- | --- | --- |
| Git copy (GitHub Desktop: Clone a Repository) | Yes | The **Update available** button pulls new code | The team |
| Stand-alone build (.exe / .app) | No | Links to the download page | Someone who only runs requests |
| ZIP download from GitHub | No | By hand | Avoid this one |

1. Install Python 3 and git. GitHub Desktop is the easy way to get git and sign in.
2. In GitHub Desktop, sign in with your work account, then clone the work repo.
3. Open the folder and double-click **Launch API Client.bat** (Windows) or **Launch API Client.command** (Mac). The first run sets itself up, which takes a minute.
4. The app opens in your browser. **Create Desktop Shortcut** gives you an icon for next time.
5. Pick or create an environment (top right) and add the district's keys there.

The red power button (top right) stops the app. The app only picks up new code when it starts, so restart it after an update.

## Tour of the code

All the Python is in the `restclient` folder. Each file does one job, and `ui.py` (the screen) calls the rest.

**Top-level folders and files**

| Item | What it is |
| --- | --- |
| `restclient/` | The app's code (see the next table) |
| `collections/` | The team's shared requests, one file per request |
| `examples/` | Example environments and sample CSVs |
| `tests/` | Automatic checks that the app still works |
| `Launch API Client.bat / .command` | Double-click to start the app |
| `build_exe.bat`, `build_app.command` | Build the stand-alone app |
| `README.md` | The full manual |

**The `restclient` folder**

| File | What it does |
| --- | --- |
| `ui.py` | Everything you see: sidebar, tabs, buttons, dialogs |
| `engine.py` | Sends a request: fills in `{{variables}}`, adds sign-in (OAuth 1, OAuth 2, bearer), follows pages |
| `variables.py` | Works out which value a `{{variable}}` gets |
| `models.py` | Defines what a collection, folder, request and environment look like |
| `collection_files.py` | Reads and writes collections as one file per request |
| `storage.py`, `workspace.py` | Load and save collections and environments |
| `team.py` | The Team button: show, share, get and undo request changes with git |
| `runner.py` | Bulk Runner: runs one request for every row of a CSV and saves the results as a CSV |
| `postman.py` | Imports from and exports to Postman |
| `secret_store.py` | Keeps keys in the Mac Keychain or Windows Credential Manager |
| `redact.py` | Blanks out keys and tokens in anything copied, saved, logged or sent to the AI |
| `support.py` | Error tips, Copy for ticket, connection test, recent lookups |
| `updater.py` | Check for updates |
| `launcher.py` | The Database tab |
| `assistant.py`, `knowledge.py`, `ollama.py`, `ollama_service.py` | The optional local AI helper |
| `mcp_server.py`, `copilot_setup.py` | Let an AI tool (such as Copilot) use the app's requests |
| `cli.py` | Runs requests from a terminal |
| `config.py` | Settings, and where files are kept |

## How requests are stored

Each request is its own small file in `collections/`, so two people can change different requests without getting in each other's way. The folders on disk match the tree in the app's sidebar.

```
collections/
  Schoology API/            one folder per collection
    _collection.json        collection name, sign-in, variables, order
    Users/                  one folder per folder
      _folder.json          folder name, order
      Get user.json         one file per request
```

A request file is short enough to edit by hand, in a text editor or on GitHub:

```json
{
  "name": "Get user",
  "method": "GET",
  "url": "{{base_url}}/users/{{user_id}}",
  "params": [{"key": "extended", "value": "true"}],
  "id": "e7a33968ded2"
}
```

- Only `name`, `method` and `url` are required. The optional fields are `description`, `params`, `headers`, `body_mode`, `body`, `form` and `auth`.
- A new file with no `id` still shows up. It's listed after the other items, A to Z.
- A file with a typo is skipped, and the app tells you which one. The rest still load.
- Deleted requests are copied to `SchoologyAPIClient/trash` first, so nothing is lost by accident.
- Saving only rewrites the files that really changed, so git shows only the real edits.

Postman imports made before this version are still in each person's old `SchoologyAPIClient/collections` folder. Bring them in with the sidebar **Import** button.

## Working together

The Team button (the people icon above the request list) shares request changes through the work repo on GitHub. Nobody needs to type git commands.

1. Add, edit, rename or delete requests as usual and click **Save**.
2. Watch the number on the Team button. Blue counts your changes that teammates don't have yet. Orange means a teammate shared something new.
3. Click Team, check the list, type a short note and click **Share my changes**. The app won't share a request that contains a real key.
4. Click **Get team changes** to pick up your teammates' changes. The app also checks GitHub every 10 minutes.
5. If you and a teammate changed the same request, the app asks which version to keep. Changes to different requests combine automatically.
6. To put a request back the way it was, click the undo arrow next to that change.

The first time you share, the app asks for your name and work email, which label your changes on GitHub.

Changes to the app's code come through the separate **Update available** button. Your unshared request edits never block it.

You can also use git directly: `git add collections`, `git commit`, `git pull --rebase`, `git push`. A pull request works too, if you'd rather review changes first.

## Safety rules

Requests are shared with the whole team through GitHub, so they hold placeholders like `{{consumer_key}}`, never real keys or district data.

| Kept where | What | Goes to GitHub? |
| --- | --- | --- |
| `collections/` in the repo | Requests: method, URL, params, headers, notes | Yes, shared with the team |
| Your environments | Base URLs, ids, district settings | No, your computer only |
| Keychain / Credential Manager | Keys, secrets, passwords | No, your computer only |
| `SchoologyAPIClient/results` | Bulk Runner CSV results | No, your computer only |
| `SchoologyAPIClient` folder | Recent lookups, logs, trash | No, your computer only |

- Put real keys in your environment and use `{{variables}}` in requests. The app refuses to share a request that contains a real key, token or password.
- Keep district names, student ids and notes about real people out of request names and descriptions.
- Keys are hidden on screen and blanked out of anything copied, saved, logged or sent to the AI.
- Use **Copy for ticket** to paste a request and its response into a ticket with the keys already removed.

## Updates, builds and tests

Code changes reach everyone through GitHub. Git copies show **Update available**, and the stand-alone app links to the newest download.

**Updates**
- A git copy checks the repo it was cloned from. When new app code is there, **Update available** pulls it. Restart the app afterwards.
- The stand-alone app checks the repo's Releases page. If the repo is private, it tells you to ask the app admin instead.
- Request changes from teammates come through the Team button, not Update available.

**Builds** (the stand-alone app, which doesn't need Python)
- On GitHub:
  - Every push to main runs the tests.
  - Pushing a version tag such as `v1.2.0` also builds the Windows, Intel Mac and Apple-chip Mac versions, and publishes them as a Release.
  - Clicking **Run workflow** on the Actions tab builds them without publishing.
- On your own computer: run `build_exe.bat` (Windows) or `build_app.command` (Mac).
- Your company may have turned off GitHub Actions for the org. If the Actions tab is missing, ask the GitHub admin.

**Tests**
- `tests/` holds about 100 automatic checks. They use a fake API and a fake GitHub, so they never touch real data.
- Run them before you share a code change: `.venv\Scripts\python -m pytest` on Windows, `.venv/bin/python -m pytest` on Mac.

## Common questions

Most problems come from one of three things:
- an old copy of the app is still running;
- the copy isn't from git;
- GitHub needs you to sign in.

| Problem | Fix |
| --- | --- |
| No Team button | Start the app from your git copy (Launch API Client), not the .exe/.app or a ZIP download. Close any older copy first. Make sure git is installed. |
| Push says "Invalid username or token" | GitHub doesn't accept passwords here. Sign in once through GitHub Desktop (Settings → Accounts) with your work account. |
| Push says 403 or permission denied | Ask the repo owner for Write access (repo Settings → Collaborators and teams). |
| Error mentions SAML or SSO | On github.com, go to your profile → Settings → Applications, then click Grant next to the org for GitHub Desktop. |
| Share refused: real key found | Replace the key in that request with a `{{variable}}`, put the key in your environment, save, and share again. |
| My old Postman imports are missing | They're in your old `SchoologyAPIClient/collections` folder. Use the sidebar Import button. |
| A request file won't load | The app names the file. Fix the typo in it, or undo the change from the Team button. |
| Where did my CSV results go? | `SchoologyAPIClient/results`, or the Open folder button in the Bulk Runner. |
| I deleted something by mistake | If you haven't shared it yet, undo it from the Team button. Otherwise copy it back from `SchoologyAPIClient/trash`. |
