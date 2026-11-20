"""Shared test set-up: tests never touch the real Mac Keychain / Windows Credential Manager."""

import os
import tempfile

os.environ["RESTCLIENT_KEYCHAIN"] = "0"  # test_secret_store.py switches it on with an in-memory keyring
os.environ.setdefault("RESTCLIENT_HOME", tempfile.mkdtemp(prefix="restclient_test_"))
