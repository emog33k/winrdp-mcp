"""Test fixtures: isolate the vault/inventory under a temp WINRDP_HOME per session."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_home():
    d = tempfile.mkdtemp(prefix="winrdp-test-")
    os.environ["WINRDP_HOME"] = d
    os.environ.setdefault("WINRDP_VAULT_KEY", "test-passphrase-not-secret")
    yield d
