"""Encrypted files in the app folder (vault.py) and hidden personal data in responses (privacy.py)."""

import json

import pytest

from restclient import privacy, runner, secret_store, support, vault
from restclient.config import Settings
from restclient.models import Environment, KV
from restclient.storage import Store


@pytest.fixture
def keychain(monkeypatch):
    """A fake Mac Keychain / Windows Credential Manager."""
    store: dict[str, str] = {}
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "get", lambda account: store.get(account))
    monkeypatch.setattr(secret_store, "put", lambda account, value: store.__setitem__(account, value))
    monkeypatch.setattr(secret_store, "delete", lambda account: store.pop(account, None))
    vault.reset_cache()
    yield store
    vault.reset_cache()


# ------------------------------------------------------------------ encrypted files
def test_files_are_encrypted_and_read_back(tmp_path, keychain):
    path = tmp_path / "x.json"
    assert vault.write_text(path, '{"email": "pat@example.test"}')
    raw = path.read_bytes()
    assert raw.startswith(vault.MAGIC) and b"pat@example.test" not in raw
    assert vault.read_text(path) == '{"email": "pat@example.test"}'
    assert vault.KEY_ACCOUNT in keychain  # the key lives in the keychain, never next to the files


def test_file_from_another_computer_cannot_be_opened(tmp_path, keychain):
    path = tmp_path / "x.json"
    vault.write_text(path, "secret stuff")
    keychain.clear()  # another computer: a different key
    vault.reset_cache()
    with pytest.raises(vault.VaultError):
        vault.read_text(path)


def test_older_plain_files_are_converted(tmp_path, keychain):
    path = tmp_path / "old.csv"
    path.write_bytes(b"row,email\n1,pat@example.test\n")
    assert vault.encrypt_in_place(path)
    assert b"pat@example.test" not in path.read_bytes()
    assert vault.read_text(path) == "row,email\n1,pat@example.test\n"
    assert not vault.encrypt_in_place(path)  # already done


def test_environments_are_encrypted(tmp_path, keychain):
    s = Settings(collections_dir=str(tmp_path / "c"), environments_dir=str(tmp_path / "e"))
    (tmp_path / "e").mkdir()
    old = Environment(name="District", variables=[KV("base_url", "https://district.example.test"),
                                                  KV("user_id", "12345")])
    (tmp_path / "e" / "District.json").write_text(json.dumps(old.to_dict()), encoding="utf-8")  # older, plain
    loaded = Store(s).load_environments()
    raw = (tmp_path / "e" / "District.json").read_bytes()
    assert raw.startswith(vault.MAGIC) and b"district.example.test" not in raw
    assert {v.key: v.value for v in loaded[0].variables}["user_id"] == "12345"
    again = Store(s).load_environments()[0]
    assert {v.key: v.value for v in again.variables}["base_url"] == "https://district.example.test"


def test_recent_lookups_and_results_are_encrypted(tmp_path, keychain, monkeypatch):
    monkeypatch.setattr(support, "recent_path", lambda: tmp_path / "recent_lookups.json")
    vault.write_text(support.recent_path(), json.dumps([{"label": "Get user", "values": {"user_id": "12345"}}]))
    assert b"12345" not in support.recent_path().read_bytes()
    assert support.load_recent()[0]["values"]["user_id"] == "12345"

    s = Settings(results_dir=str(tmp_path / "r"))
    step = runner.StepResult(row=1, request="Get user", method="GET", url="https://x.test/users/1", status=200,
                             passed=True, elapsed_ms=5, extracted={"email": "pat@example.test", "uid": "77"},
                             response='{"id": 1, "primary_email": "pat@example.test"}')
    path = runner.save_results([step], s, "run")
    assert b"pat@example.test" not in path.read_bytes() and b"77" not in path.read_bytes()
    text = runner.read_saved_results(path)
    assert "77" in text and "pat@example.test" not in text  # personal fields hidden even inside the file


def test_without_a_keychain_files_stay_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "available", lambda: False)
    path = tmp_path / "x.json"
    assert not vault.write_text(path, "{}")
    assert vault.read_text(path) == "{}"


# ------------------------------------------------------------------ hidden personal data
ONEROSTER_USER = {"user": {"sourcedId": "u-1", "status": "active", "username": "pat.lee", "givenName": "Pat",
                           "familyName": "Lee", "email": "pat@example.test", "sms": "555-0100", "role": "student",
                           "orgs": [{"sourcedId": "s-1", "type": "org"}],
                           "userIds": [{"type": "email", "identifier": "pat@example.test"}],
                           "metadata": {"birthDate": "2010-01-02"}}}
SCHOOLOGY_USER = {"id": 12345, "school_id": 99, "school_uid": "S-1", "name_first": "Pat", "name_last": "Lee",
                  "name_display": "Pat Lee", "primary_email": "pat@example.test", "birthday_date": "2010-01-02",
                  "phone": "555-0100", "picture_url": "https://x.test/p.jpg", "role_id": 3}


def test_personal_fields_hidden_ids_kept():
    text = privacy.mask_text(json.dumps(ONEROSTER_USER))
    for personal in ("Pat", "Lee", "pat@example.test", "555-0100", "2010-01-02", "pat.lee"):
        assert personal not in text
    for kept in ("u-1", "s-1", "student", "active"):
        assert kept in text
    text = privacy.mask_text(json.dumps(SCHOOLOGY_USER))
    for personal in ("Pat", "pat@example.test", "2010-01-02", "555-0100", "p.jpg"):
        assert personal not in text
    assert "12345" in text and "S-1" in text


def test_show_personal_data_still_hides_secrets():
    body = json.dumps({"access_token": "abc.def.ghi", "token_type": "bearer", "expires_in": 3600,
                       "user": {"email": "pat@example.test", "password": "Hunter2!"}})
    shown = privacy.mask_text(body, hide_personal=False)
    assert "pat@example.test" in shown
    assert "Hunter2!" not in shown and "abc.def.ghi" not in shown
    assert "bearer" in shown and "3600" in shown


def test_xml_and_school_names():
    xml = "<user><uid>9</uid><name_first>Pat</name_first><primary_email>p@x.test</primary_email></user>"
    out = privacy.mask_text(xml)
    assert "Pat" not in out and "p@x.test" not in out and "<uid>9</uid>" in out
    school = json.dumps({"org": {"sourcedId": "s-1", "name": "Lincoln High", "type": "school"}})
    assert "Lincoln High" in privacy.mask_text(school)  # school names aren't personal data
    assert not privacy.has_personal_data(school) and privacy.has_personal_data(json.dumps(SCHOOLOGY_USER))
