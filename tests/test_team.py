"""One file per request + sharing collections through git (two teammates, one fake GitHub)."""

import json
import subprocess

import pytest

from restclient import collection_files as cf
from restclient import team, updater
from restclient.config import Settings
from restclient.models import Auth, Collection, Folder, KV, Request
from restclient.storage import Store


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _sample() -> Collection:
    return Collection(id="coll1", name="Schoology API", variables=[KV("base_url", "https://api.example.test/v1")],
                      auth=Auth(type="oauth1", consumer_key="{{key}}", consumer_secret="{{secret}}"),
                      items=[Folder(id="f1", name="Users", items=[
                          Request(id="r1", name="Get user", url="{{base_url}}/users/{{user_id}}"),
                          Request(id="r2", name="Who am I", url="{{base_url}}/app-user-info")]),
                          Request(id="r3", name="List schools", url="{{base_url}}/schools",
                                  params=[KV("limit", "20", enabled=False)])])


# ------------------------------------------------------------------ file format
def test_one_file_per_request_round_trip(tmp_path):
    c = _sample()
    folder = tmp_path / "Schoology API"
    cf.write_collection(c, folder)
    assert (folder / "Users" / "Get user.json").exists() and (folder / "List schools.json").exists()
    data = json.loads((folder / "Users" / "Get user.json").read_text(encoding="utf-8"))
    assert data == {"name": "Get user", "method": "GET", "url": "{{base_url}}/users/{{user_id}}", "id": "r1"}
    back, problems = cf.read_collection(folder)
    assert problems == [] and back.to_dict() == c.to_dict()
    assert cf.write_collection(back, folder) == []  # nothing changed -> no file touched

    back.items[0].items[0].url += "?x=1"  # edit one request: only its file changes
    assert cf.write_collection(back, folder) == ["Users/Get user.json"]


def test_hand_added_and_removed_files(tmp_path):
    folder = tmp_path / "Schoology API"
    cf.write_collection(_sample(), folder)
    (folder / "Users" / "Find by email.json").write_text(json.dumps(
        {"name": "Find by email", "method": "GET", "url": "{{base_url}}/users?email={{email}}"}))
    (folder / "Users" / "Who am I.json").unlink()  # a teammate deleted it (it's still listed in "order")
    (folder / "List schools.json").write_text("{ broken")
    c, problems = cf.read_collection(folder)
    names = [r.name for _, r in c.walk()]
    assert names == ["Get user", "Find by email"]
    assert len(problems) == 1 and "List schools.json" in problems[0]
    again, _ = cf.read_collection(folder)
    assert again.find_request("Users/Find by email").id == c.find_request("Users/Find by email").id  # stable


def test_copied_file_is_its_own_request(tmp_path):
    folder = tmp_path / "Schoology API"
    cf.write_collection(_sample(), folder)
    original = folder / "Users" / "Get user.json"
    (folder / "Users" / "Get user copy.json").write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    c, _ = cf.read_collection(folder)
    ids = [r.id for _, r in c.walk()]
    assert len(ids) == len(set(ids))  # same "id" in both files: still two separate requests
    copy = c.find_request("Users/Get user (2)") or [r for _, r in c.walk() if r.id != "r1" and r.name == "Get user"][0]
    copy.body_mode, copy.body = "json", '{"only": "the copy"}'
    cf.write_collection(c, folder)
    again, _ = cf.read_collection(folder)
    assert again.find_request("r1").body == ""


def test_duplicate_names_get_separate_files(tmp_path):
    c = Collection(name="C", items=[Request(name="Same"), Request(name="Same"), Request(name="_folder")])
    files = cf.collection_files(c)
    assert {"Same.json", "Same (2).json", "_folder (2).json"} <= set(files)


def test_store_converts_old_one_file_collections(tmp_path):
    s = Settings(collections_dir=str(tmp_path / "c"), environments_dir=str(tmp_path / "e"))
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "Old.json").write_text(json.dumps(_sample().to_dict()), encoding="utf-8")
    store = Store(s)
    loaded = store.load_collections()
    assert [c.name for c in loaded] == ["Schoology API"] and loaded[0].path.is_dir()
    assert not (tmp_path / "c" / "Old.json").exists()
    assert (store.trash_path() / "Old.json").exists()
    assert Store(s).load_collections()[0].to_dict() == _sample().to_dict()


def test_rename_and_delete_request_via_store(tmp_path):
    s = Settings(collections_dir=str(tmp_path / "c"), environments_dir=str(tmp_path / "e"))
    store = Store(s)
    c = _sample()
    store.save_collection(c)
    c.items[0].items[0].name = "Get one user"
    c.remove_item("r3")
    store.save_collection(c)
    folder = tmp_path / "c" / "Schoology API"
    assert (folder / "Users" / "Get one user.json").exists()
    assert not (folder / "Users" / "Get user.json").exists() and not (folder / "List schools.json").exists()
    assert (store.trash_path() / "Schoology API" / "List schools.json").exists()


# ------------------------------------------------------------------ git sharing
@pytest.fixture
def teammates(tmp_path, monkeypatch):
    """Two clones (me, mate) of one bare repo, with a collections/ folder. Returns a function to switch."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))  # ignore the real computer's git settings
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clones = {}
    for who in ("me", "mate"):
        path = tmp_path / who
        _git("clone", str(origin), str(path), cwd=tmp_path)
        _git("config", "user.name", who, cwd=path)
        _git("config", "user.email", f"{who}@example.test", cwd=path)
        clones[who] = path
    cf.write_collection(_sample(), clones["me"] / "collections" / "Schoology API")
    (clones["me"] / "app.py").write_text("x = 1\n")
    _git("add", ".", cwd=clones["me"])
    _git("commit", "-m", "first", cwd=clones["me"])
    _git("push", "-u", "origin", "HEAD:main", cwd=clones["me"])
    _git("pull", cwd=clones["mate"])
    _git("branch", "--set-upstream-to=origin/main", cwd=clones["mate"])

    def be(who):
        monkeypatch.setattr(team, "repo", lambda: clones[who])
        return clones[who]

    return be


def _coll(root):
    c, problems = cf.read_collection(root / "collections" / "Schoology API")
    assert problems == []
    return c


def _save(root, c):
    cf.write_collection(c, root / "collections" / "Schoology API")


def test_share_and_get_team_changes(teammates):
    me = teammates("me")
    c = _coll(me)
    c.find_request("r1").url += "?fields=email"
    c.items.append(Request(id="r9", name="New lookup", url="{{base_url}}/x"))
    _save(me, c)
    changes = team.local_changes()
    assert [(ch.kind, ch.label) for ch in changes] == [  # appended at the end: the order list needn't change
        ("added", "Schoology API › New lookup"),
        ("edited", "Schoology API › Users › Get user")]
    assert team.suggest_message(changes) == "Add New lookup; Update Get user"
    assert team.share("") == "Shared 2 change(s) with the team."
    assert team.local_changes() == [] and team.status().unsent == 0

    mate = teammates("mate")
    st = team.status()
    assert st.incoming == ["Add New lookup; Update Get user (me)"]
    files = team.get_latest()
    assert "collections/Schoology API/New lookup.json" in files
    assert _coll(mate).find_request("r1").url.endswith("?fields=email")


def test_both_edit_same_request_then_choose(teammates):
    mate = teammates("mate")
    c = _coll(mate)
    c.find_request("r2").description = "mate's note"
    _save(mate, c)
    team.share("mate edit")

    me = teammates("me")
    c = _coll(me)
    c.find_request("r2").description = "my note"
    _save(me, c)
    with pytest.raises(team.Conflict) as err:
        team.get_latest()  # my unshared edit clashes with theirs: ask first, change nothing
    assert "Users › Who am I" in str(err.value)
    with pytest.raises(team.Conflict):
        team.share("my edit")
    assert team.status(fetch=False).unsent == 1  # my change is saved locally, nothing lost
    assert team.share("my edit", prefer="mine").startswith("Shared")
    assert _coll(me).find_request("r2").description == "my note"

    mate = teammates("mate")
    team.get_latest()
    assert _coll(mate).find_request("r2").description == "my note"


def test_keep_teammates_version(teammates):
    mate = teammates("mate")
    c = _coll(mate)
    c.find_request("r2").description = "mate's note"
    _save(mate, c)
    team.share("")
    me = teammates("me")
    c = _coll(me)
    c.find_request("r2").description = "my note"
    _save(me, c)
    team.get_latest(prefer="theirs")
    assert _coll(me).find_request("r2").description == "mate's note"
    assert team.local_changes() == []


def test_different_requests_merge_without_asking(teammates):
    mate = teammates("mate")
    c = _coll(mate)
    c.find_request("r1").description = "mate"
    _save(mate, c)
    team.share("")
    me = teammates("me")
    c = _coll(me)
    c.find_request("r3").description = "me"
    _save(me, c)
    team.get_latest()  # unshared edit of a different file is kept
    c = _coll(me)
    assert c.find_request("r1").description == "mate" and c.find_request("r3").description == "me"
    assert [ch.label for ch in team.local_changes()] == ["Schoology API › List schools"]


def test_secrets_never_shared(teammates):
    me = teammates("me")
    c = _coll(me)
    c.find_request("r1").auth = Auth(type="bearer", token="real-token-123456")
    _save(me, c)  # the app never writes the secret into the file
    assert "real-token-123456" not in (me / "collections" / "Schoology API" / "Users" / "Get user.json").read_text()
    # a file written by an older version, or by hand, is still stopped
    cf.write_collection(c, me / "collections" / "Schoology API", strip_secrets=False)
    with pytest.raises(team.TeamError, match="real keys or secrets"):
        team.share("")
    assert team.status(fetch=False).unsent == 0


def test_name_needed_once(teammates, tmp_path):
    me = teammates("me")
    _git("config", "--unset", "user.name", cwd=me)
    _git("config", "--unset", "user.email", cwd=me)
    c = _coll(me)
    c.find_request("r1").description = "x"
    _save(me, c)
    with pytest.raises(team.NeedIdentity):
        team.share("")
    team.set_identity("Pat", "pat@example.test")
    assert team.share("").startswith("Shared")


def test_undo(teammates):
    me = teammates("me")
    c = _coll(me)
    c.find_request("r1").description = "oops"
    c.items.append(Request(id="r9", name="Scratch", url="x"))
    _save(me, c)
    for ch in team.local_changes():
        team.undo(ch.path)
    assert team.local_changes() == []
    assert _coll(me).find_request("r1").description == ""
    with pytest.raises(team.TeamError):
        team.undo("app.py")


def test_app_update_keeps_unshared_request_edits(teammates, monkeypatch):
    mate = teammates("mate")
    (mate / "app.py").write_text("x = 2\n")
    _git("commit", "-am", "App fix", cwd=mate)
    _git("push", cwd=mate)
    me = teammates("me")
    c = _coll(me)
    c.find_request("r1").description = "mine"
    _save(me, c)
    monkeypatch.setattr(updater, "source_folder", lambda: me)
    st = updater.check()
    assert st.available and st.can_apply  # request edits don't block app updates
    updater.apply()
    assert (me / "app.py").read_text() == "x = 2\n"
    assert _coll(me).find_request("r1").description == "mine"


def test_teammates_hand_written_file_isnt_rewritten(teammates):
    mate = teammates("mate")
    (mate / "collections" / "Schoology API" / "Users" / "By email.json").write_text(
        '{"name": "By email", "method": "GET", "url": "{{base_url}}/users?email={{email}}"}', encoding="utf-8")
    team.share("hand-written")
    me = teammates("me")
    team.get_latest()
    c = _coll(me)
    c.find_request("r3").description = "only this"
    _save(me, c)
    assert [ch.label for ch in team.local_changes()] == ["Schoology API › List schools"]


def test_request_changes_dont_count_as_app_updates(teammates, monkeypatch):
    mate = teammates("mate")
    c = _coll(mate)
    c.find_request("r1").description = "x"
    _save(mate, c)
    team.share("")
    me = teammates("me")
    monkeypatch.setattr(updater, "source_folder", lambda: me)
    assert not updater.check().available
    assert team.status().behind == 1


def test_passwords_in_a_body_are_caught():
    assert team.secrets_in({"body": '{"user": "pat", "password": "Hunter2!"}'}) == ["body password"]
    assert team.secrets_in({"body": "username=pat&password=Hunter2!"}) == ["body password"]
    assert team.secrets_in({"form": [{"key": "client_secret", "value": "abc123"}]}) == ["form client_secret"]
    assert team.secrets_in({"body": '{"password": "{{password}}", "grant_type": "client_credentials"}'}) == []
    assert team.secrets_in({"params": [{"key": "limit", "value": "20"}]}) == []


def test_no_real_secrets_in_the_shared_collections():
    """Runs on every push, so a key or password added straight on GitHub fails the check."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "collections"
    found = []
    for path in sorted(root.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        bad = team.secrets_in(data)
        if bad:
            found.append(f"{path.relative_to(root).as_posix()}: {', '.join(map(str, bad))}")
    assert not found, "Real keys or passwords in request files (use {{variables}}):\n" + "\n".join(found)
