"""The real Streamlit page with a password hash set: nothing shows until the password is right."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from mmrisk.security.passwords import hash_password  # noqa: E402

APP = Path(__file__).resolve().parents[2] / "src" / "mmrisk" / "dashboard" / "app.py"


def page(monkeypatch, with_password):
    if with_password:
        monkeypatch.setenv("MMRISK_DASH_PASSWORD_HASH", hash_password("a long enough pass"))
    else:
        monkeypatch.delenv("MMRISK_DASH_PASSWORD_HASH", raising=False)
    return AppTest.from_file(str(APP), default_timeout=60).run()


def test_the_page_shows_only_a_login_until_the_password_is_right(monkeypatch):
    at = page(monkeypatch, True)
    assert [t.label for t in at.text_input] == ["Password"] and len(at.tabs) == 0
    at.text_input[0].set_value("wrong wrong wrong")
    at.button[0].click().run()
    assert [e.value for e in at.error] == ["wrong password"] and len(at.tabs) == 0
    at.text_input[0].set_value("a long enough pass")
    at.button[0].click().run()
    assert len(at.tabs) == 4 and not at.exception and not at.text_input


def test_without_a_hash_the_page_opens_directly(monkeypatch):
    at = page(monkeypatch, False)
    assert len(at.tabs) == 4 and not at.exception
