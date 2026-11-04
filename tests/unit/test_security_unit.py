"""Phase 18 building blocks: keys, passwords, rate limits, audit log, scanner, dashboard gate."""

import json
import os
import stat
import sys

import pytest

from mmrisk.dashboard.gate import LIMIT, WINDOW, Gate
from mmrisk.security import passwords
from mmrisk.security.audit import AuditLog
from mmrisk.security.cli import main as sec_main
from mmrisk.security.keys import KeyFileError, KeyStore
from mmrisk.security.ratelimit import FailureLimiter, RateLimitError, TokenBucket
from mmrisk.security.scan import scan


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---- keys ----


def test_a_key_is_shown_once_and_only_its_hash_is_stored(tmp_path):
    store = KeyStore(tmp_path / "k.json")
    token, record = store.create("ops", "score")
    assert token.startswith("mmk_" + record["id"] + "_")
    raw = (tmp_path / "k.json").read_text()
    assert token not in raw and token.split("_", 2)[2] not in raw and "sha256" in raw
    assert "sha256" not in json.dumps(store.keys())
    assert store.check(token)["role"] == "score"


@pytest.mark.parametrize("bad", ["", "mmk_", "x", "mmk_00000000_" + "A" * 43, None])
def test_wrong_tokens_are_refused(tmp_path, bad):
    store = KeyStore(tmp_path / "k.json")
    store.create("ops", "admin")
    assert store.check(bad) is None


def test_a_token_with_the_right_id_and_a_wrong_secret_is_refused(tmp_path):
    store = KeyStore(tmp_path / "k.json")
    token, rec = store.create("ops", "admin")
    forged = f"mmk_{rec['id']}_" + "A" * 43
    assert forged != token and store.check(forged) is None


def test_revoking_stops_a_key_and_keeps_its_record(tmp_path):
    store = KeyStore(tmp_path / "k.json")
    token, rec = store.create("ops", "score")
    store.create("other", "metrics")
    store.revoke(rec["id"])
    assert store.check(token) is None and store.active_count() == 1
    assert [k["id"] for k in store.keys()].count(rec["id"]) == 1
    with pytest.raises(KeyFileError, match="already revoked"):
        store.revoke(rec["id"])
    with pytest.raises(KeyFileError, match="no key"):
        store.revoke("ffffffff")


def test_key_creation_rules(tmp_path):
    store = KeyStore(tmp_path / "k.json")
    with pytest.raises(KeyFileError, match="role"):
        store.create("a", "root")
    for name in ("", "  ", "x" * 61):
        with pytest.raises(KeyFileError, match="name"):
            store.create(name, "score")
    assert store.create("x" * 60, "score")[1]["name"] == "x" * 60


def test_a_damaged_key_file_is_an_error_not_an_empty_store(tmp_path):
    for text in ("{", "[]", '{"keys": [{"id": "a"}]}', '{"keys": 3}'):
        path = tmp_path / "k.json"
        path.write_text(text)
        with pytest.raises(KeyFileError, match="not a key file"):
            KeyStore(path).check("mmk_00000000_" + "A" * 43)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_key_file_is_private_to_the_owner(tmp_path):
    KeyStore(tmp_path / "k.json").create("ops", "score")
    assert stat.S_IMODE((tmp_path / "k.json").stat().st_mode) == 0o600


# ---- passwords ----


def test_passwords_round_trip_and_hashes_are_salted():
    a, b = (
        passwords.hash_password("correct horse battery"),
        passwords.hash_password("correct horse battery"),
    )
    assert a != b and a.startswith("scrypt$")
    assert passwords.verify("correct horse battery", a) and not passwords.verify(
        "correct horse batterx", a
    )
    assert not passwords.verify("", a)
    assert "correct" not in a


def test_short_passwords_and_unusable_hashes_are_refused():
    with pytest.raises(passwords.PasswordError, match="12"):
        passwords.hash_password("short")
    assert passwords.hash_password("x" * passwords.MIN_LENGTH)
    for bad in ("", "scrypt$1$1$1$a$b", "md5$16384$8$1$AAAA$AAAA", "scrypt$16384$8$1$!!$!!",
                "scrypt$1099511627776$8$1$AAAA$AAAA"):  # fmt: skip
        with pytest.raises(passwords.PasswordError):
            passwords.verify("anything at all", bad)


# ---- rate limits ----


def test_the_bucket_allows_a_burst_then_refills_with_time():
    clock = Clock()
    bucket = TokenBucket(60, 3, clock)  # one per second
    assert [bucket.allow("k")[0] for _ in range(4)] == [True, True, True, False]
    ok, wait = bucket.allow("k")
    assert not ok and wait == pytest.approx(1.0)
    clock.t += 1.0
    assert bucket.allow("k")[0] and not bucket.allow("k")[0]
    clock.t += 1000
    assert [bucket.allow("k")[0] for _ in range(4)] == [True, True, True, False]  # capped at burst
    assert bucket.allow("other")[0]  # keys do not share a bucket


def test_the_bucket_forgets_old_names_so_memory_stays_bounded():
    bucket = TokenBucket(60, 1, Clock())
    from mmrisk.security import ratelimit

    for i in range(ratelimit.MAX_TRACKED + 50):
        bucket.allow(f"k{i}")
    assert len(bucket._state) == ratelimit.MAX_TRACKED


def test_impossible_limits_are_refused():
    for args in ((0, 1), (-1, 1), (10, 0)):
        with pytest.raises(RateLimitError):
            TokenBucket(*args)
    for args in ((0, 10), (3, 0)):
        with pytest.raises(RateLimitError):
            FailureLimiter(*args)


def test_failures_block_at_the_limit_and_expire_after_the_window():
    clock = Clock()
    f = FailureLimiter(3, 60, clock)
    for _ in range(2):
        f.fail("c")
    assert not f.blocked("c") and f.retry_after("c") == 0
    f.fail("c")
    assert f.blocked("c") and f.retry_after("c") == pytest.approx(60)
    assert not f.blocked("other")
    clock.t += 59
    assert f.blocked("c") and f.retry_after("c") == pytest.approx(1)
    clock.t += 1
    assert not f.blocked("c")
    f.fail("c")
    f.reset("c")
    assert not f.blocked("c")


# ---- audit log ----


def test_the_audit_log_records_fields_and_refuses_unknown_ones(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.record("auth_failed", route="/v1/score", status=401, client="1.2.3.4", detail=None)
    row = log.read()[0]
    assert row["event"] == "auth_failed" and row["status"] == 401 and "detail" not in row
    with pytest.raises(ValueError, match="body"):
        log.record("x", body="secret")


def test_the_audit_log_rotates_once_and_survives_damage(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", max_bytes=300)
    for i in range(30):
        log.record("e", detail=str(i))
    assert (tmp_path / "a.jsonl.1").exists() and not (tmp_path / "a.jsonl.2").exists()
    assert (tmp_path / "a.jsonl").stat().st_size <= 300
    with open(tmp_path / "a.jsonl", "a") as fh:
        fh.write("{broken\n")
    assert log.read()[-1] == {"event": "unreadable_line"}
    assert AuditLog(tmp_path / "none.jsonl").read() == []


# ---- dashboard gate ----


def test_the_gate_accepts_the_password_and_locks_after_repeated_failures():
    clock = Clock()
    gate = Gate(passwords.hash_password("a long enough pass"), clock)
    assert gate.attempt("a long enough pass") == (True, "")
    for _ in range(LIMIT):
        assert gate.attempt("wrong password!!")[1] == "wrong password"
    ok, message = gate.attempt("a long enough pass")  # even the right one waits
    assert not ok and "try again" in message
    clock.t += WINDOW + 1
    assert gate.attempt("a long enough pass")[0]


def test_a_success_clears_the_failure_count_and_a_bad_hash_fails_at_start():
    gate = Gate(passwords.hash_password("a long enough pass"), Clock())
    for _ in range(LIMIT - 1):
        gate.attempt("nope nope nope")
    assert gate.attempt("a long enough pass")[0]
    for _ in range(LIMIT - 1):
        gate.attempt("nope nope nope")
    assert gate.attempt("a long enough pass")[0]
    with pytest.raises(passwords.PasswordError):
        Gate("not a hash")


# ---- scanner ----


def _git_free(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_the_scanner_finds_credentials_and_previews_them_briefly(tmp_path):
    root = _git_free(tmp_path, {
        "a.py": 'pass' + 'word = "hunter2hunter2hunter2"\n',
        "b.txt": "-----BEGIN RSA " + "PRIVATE KEY-----\n",
        "c.txt": "id AKIA" + "ABCDEFGHIJKLMNOP\n",
        "d.txt": "t ghp_" + "a" * 36 + "\n",
        "e.txt": "k mmk_0123abcd_" + "A" * 43 + "\n",
    })  # fmt: skip
    found = {(f.path, f.rule) for f in scan(root)}
    assert found == {("a.py", "literal secret"), ("b.txt", "private key"),
                     ("c.txt", "aws access key id"), ("d.txt", "github token"),
                     ("e.txt", "project api key")}  # fmt: skip
    assert all(len(f.preview) <= 5 for f in scan(root))


def test_the_scanner_flags_files_that_must_not_be_committed(tmp_path):
    root = _git_free(tmp_path, {".env": "A=1", "x/server.pem": "x", "api_keys.json": "{}",
                                ".env.example": "A=", "ok.txt": "fine"})  # fmt: skip
    assert {f.path for f in scan(root)} == {".env", "x/server.pem", "api_keys.json"}


def test_a_folder_inside_another_repository_is_not_hidden_by_that_repos_ignore_rules(tmp_path):
    import shutil
    import subprocess as sp

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env\n*.pem\n")
    inner = _git_free(tmp_path / "inner", {".env": "A=1", "s.pem": "x"})
    assert {f.path for f in scan(inner)} == {".env", "s.pem"}  # the outer repo ignores both


def test_the_scanner_ignores_placeholders_markers_and_generated_folders(tmp_path):
    root = _git_free(tmp_path, {
        "a.py": 'password = "changeme-changeme"\ntoken = os.environ["T"]  \n'
                'secret = "real-looking-value-1"  # nosecret\napi_key = "<your-api-key-here>"\n',
        "data/x.txt": "-----BEGIN " + "PRIVATE KEY-----",
        ".venv/lib/x.py": 'pass' + 'word = "hunter2hunter2hunter2"',
        "big.txt": "AKIA" + "ABCDEFGHIJKLMNOP" + " " * 1_100_000,
        "bin.dat": "ok",
    })  # fmt: skip
    (root / "bin.dat").write_bytes(b"\xff\xfe AKIA" + b"ABCDEFGHIJKLMNOP")
    assert scan(root) == []


def test_the_repository_itself_is_clean():
    from pathlib import Path

    assert scan(Path(__file__).resolve().parents[2]) == []


# ---- the security command ----


def test_the_command_makes_lists_and_revokes_keys_and_writes_the_audit_log(tmp_path, capsys):
    k, a = str(tmp_path / "k.json"), str(tmp_path / "a.jsonl")
    base = ["keys", "--file", k, "--audit-log", a]
    assert sec_main([*base, "list"]) == 0 and "no keys" in capsys.readouterr().out
    assert sec_main([*base, "create", "--name", "ops", "--role", "score"]) == 0
    token = capsys.readouterr().out.split()[-1]
    assert KeyStore(tmp_path / "k.json").check(token)
    assert sec_main([*base, "list"]) == 0 and "active" in capsys.readouterr().out
    key_id = KeyStore(tmp_path / "k.json").keys()[0]["id"]
    assert sec_main([*base, "revoke", key_id]) == 0
    assert sec_main([*base, "revoke", key_id]) == 2 and "already revoked" in capsys.readouterr().err
    events = [r["event"] for r in AuditLog(tmp_path / "a.jsonl").read()]
    assert events == ["key_created", "key_revoked"]
    assert token not in (tmp_path / "a.jsonl").read_text()
    assert sec_main(["audit", "--file", a, "--last", "1"]) == 0
    assert "key_revoked" in capsys.readouterr().out


def test_the_password_command_writes_a_hash_and_checks_both_entries(tmp_path, monkeypatch, capsys):
    out = tmp_path / "d" / "dash.hash"
    answers = iter(["a long enough pass", "a long enough pass"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert sec_main(["dash-password", "--out", str(out)]) == 0
    assert passwords.verify("a long enough pass", out.read_text())
    assert "a long enough pass" not in capsys.readouterr().out
    answers = iter(["a long enough pass", "another long pass!"])
    assert sec_main(["dash-password", "--out", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x").exists()


def test_the_scan_command_sets_the_exit_code(tmp_path, capsys):
    (tmp_path / "ok.txt").write_text("fine")
    assert sec_main(["scan", "--root", str(tmp_path)]) == 0
    (tmp_path / ".env").write_text("A=1")
    assert sec_main(["scan", "--root", str(tmp_path)]) == 1
    assert ".env" in capsys.readouterr().out


def test_running_the_module_works():
    import subprocess

    done = subprocess.run([sys.executable, "-m", "mmrisk.security", "--help"],
                          capture_output=True, text=True, timeout=60)  # fmt: skip
    assert done.returncode == 0 and "keys" in done.stdout
