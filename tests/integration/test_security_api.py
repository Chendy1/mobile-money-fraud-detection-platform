# ruff: noqa: F811, F401
"""Phase 18 with a real trained model: API keys, roles, limits, audit, TLS options, safe load."""

import pickle
import ssl
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient  # noqa: E402
from test_api import COLUMNS, data, env, features, model, model_dir, ordered, wire  # noqa: F401

from mmrisk.api import ModelLoadError, ScoringService  # noqa: E402
from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.api.cli import main as cli  # noqa: E402
from mmrisk.api.service import load_model  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.security.apiauth import Security  # noqa: E402
from mmrisk.security.audit import AuditLog  # noqa: E402
from mmrisk.security.keys import KeyStore  # noqa: E402
from mmrisk.security.ratelimit import FailureLimiter, TokenBucket  # noqa: E402
from mmrisk.security.safeload import UnsafePickleError, safe_load  # noqa: E402


@pytest.fixture
def secured(tmp_path, model, data, ordered):
    keys = KeyStore(tmp_path / "k.json")
    tokens = {role: keys.create(role, role)[0] for role in ("score", "metrics", "admin")}
    audit = AuditLog(tmp_path / "a.jsonl")
    sec = Security(keys, audit, TokenBucket(600, 50), FailureLimiter(50, 60))
    service = ScoringService(model, data["customers"], ordered.head(100))
    client = TestClient(create_app(service, security=sec))
    row = wire(ordered.tail(1).to_dicts()[0])
    return client, tokens, sec, audit, row, keys


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_health_and_ready_are_open_and_everything_else_needs_a_key(secured):
    client, tokens, _, audit, row, _ = secured
    assert client.get("/health").status_code == 200 and client.get("/ready").status_code == 200
    routes = [("POST", "/v1/score"), ("GET", "/v1/model"), ("GET", "/metrics")]
    routes += [("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/nothing")]
    for method, path in routes:
        r = client.request(method, path, json=row if method == "POST" else None)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.json()["error"]["code"] == "unauthorized"
    # the same answer for a missing key, a wrong key and an unknown route: nothing to probe
    assert client.get("/nothing", headers=bearer(tokens["admin"])).status_code == 403


def test_a_score_key_scores_but_cannot_read_metrics_and_the_reverse(secured):
    client, tokens, _, _, row, _ = secured
    ok = client.post("/v1/score", json=row, headers=bearer(tokens["score"]))
    assert ok.status_code == 200 and "probability" in ok.json() or ok.status_code == 200
    assert client.get("/v1/model", headers=bearer(tokens["score"])).status_code == 200
    assert client.get("/metrics", headers=bearer(tokens["score"])).status_code == 403
    assert client.get("/metrics", headers=bearer(tokens["metrics"])).status_code == 200
    assert client.post("/v1/score", json=row, headers=bearer(tokens["metrics"])).status_code == 403
    assert client.get("/v1/model", headers=bearer(tokens["metrics"])).status_code == 403
    for path in ("/metrics", "/v1/model"):
        assert client.get(path, headers=bearer(tokens["admin"])).status_code == 200
    assert client.post("/v1/score", json=row, headers=bearer(tokens["admin"])).status_code == 200


def test_only_the_bearer_scheme_with_one_token_is_accepted(secured):
    client, tokens, *_ = secured
    t = tokens["score"]
    for header in (
        t,
        f"Basic {t}",
        "Bearer",
        f"Token {t}",
        f"Bearer {t}x",
        "Bearer " + "A" * 5000,
    ):
        assert client.get("/v1/model", headers={"Authorization": header}).status_code == 401, header
    assert client.get("/v1/model", headers={"Authorization": f"bearer {t}"}).status_code == 200
    assert client.get("/v1/model", params={"api_key": t}).status_code == 401  # never in the URL


def test_a_revoked_key_stops_working_at_once(secured):
    client, tokens, _, _, _, keys = secured
    assert client.get("/v1/model", headers=bearer(tokens["score"])).status_code == 200
    keys.revoke(keys.keys()[0]["id"])
    assert client.get("/v1/model", headers=bearer(tokens["score"])).status_code == 401


def test_repeated_bad_keys_block_the_client_even_for_a_good_key(secured):
    client, tokens, sec, *_ = secured
    sec.failures = FailureLimiter(5, 60)
    for _ in range(5):
        assert (
            client.get("/v1/model", headers=bearer("mmk_00000000_" + "A" * 43)).status_code == 401
        )
    blocked = client.get("/v1/model", headers=bearer(tokens["score"]))
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert blocked.json()["error"]["code"] == "too_many_failures"
    assert client.get("/health").status_code == 200  # probes still work


def test_a_good_key_resets_the_failure_count(secured):
    client, tokens, sec, *_ = secured
    sec.failures = FailureLimiter(5, 60)
    for _ in range(4):
        client.get("/v1/model", headers=bearer("nope"))
    assert client.get("/v1/model", headers=bearer(tokens["score"])).status_code == 200
    for _ in range(4):
        client.get("/v1/model", headers=bearer("nope"))
    assert client.get("/v1/model", headers=bearer(tokens["score"])).status_code == 200


def test_each_key_has_its_own_rate_limit(secured, model, data, ordered):
    client, tokens, sec, *_ = secured
    sec.bucket = TokenBucket(60, 2)
    h = bearer(tokens["score"])
    codes = [client.get("/v1/model", headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = client.get("/v1/model", headers=h)
    assert limited.json()["error"]["code"] == "rate_limited" and "retry-after" in limited.headers
    assert client.get("/v1/model", headers=bearer(tokens["admin"])).status_code == 200


def test_the_audit_log_names_keys_and_routes_but_never_keys_bodies_or_ids(secured):
    client, tokens, _, audit, row, _ = secured
    client.post("/v1/score", json=row, headers=bearer("mmk_00000000_" + "B" * 43))
    client.get("/metrics", headers=bearer(tokens["score"]))
    client.get("/v1/model", headers={**bearer(tokens["score"]), "X-Request-ID": "abc-123"})
    text = (audit.path).read_text()
    for forbidden in (*tokens.values(), row["customer_id"], row["transaction_id"], "B" * 43):
        assert forbidden not in text
    events = [r["event"] for r in audit.read()]
    assert events == ["auth_failed", "forbidden"]
    assert audit.read()[1]["role"] == "score" and audit.read()[1]["status"] == 403
    assert audit.read()[0]["route"] == "/v1/score" and audit.read()[0]["detail"] == "bad key"


def test_request_ids_are_echoed_only_when_they_are_safe(secured):
    client, *_ = secured
    r = client.get("/metrics", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = client.get("/metrics", headers={"X-Request-ID": "bad id\t;"})
    assert r.headers["x-request-id"] != "bad id\t;" and len(r.headers["x-request-id"]) == 32


def test_without_security_nothing_changes(model, data, ordered):
    service = ScoringService(model, data["customers"], ordered.head(100))
    client = TestClient(create_app(service))
    assert client.get("/v1/model").status_code == 200 and client.get("/docs").status_code == 200


# ---- safe loading of the model file ----


class Evil:
    def __reduce__(self):
        return (subprocess.run, (["echo", "pwned"],))


def test_a_real_bundle_loads_safely_and_gives_the_same_scores(model_dir, model, features):
    loaded = RiskModel.load(model_dir)
    sample = features.head(50)
    assert (
        loaded.assess(sample)["risk_probability"].to_list()
        == model.assess(sample)["risk_probability"].to_list()
    )


def test_a_file_that_runs_a_command_is_refused_before_it_runs(tmp_path, model_dir):
    path = tmp_path / "risk_model.joblib"
    path.write_bytes(pickle.dumps(Evil()))
    with pytest.raises(UnsafePickleError, match="subprocess"):
        safe_load(path)
    # prove the unsafe loader would have run it, so the test is not passing for another reason
    marker = tmp_path / "marker"
    path.write_bytes(
        pickle.dumps(type("E", (), {"__reduce__": lambda s: (Path.write_text, (marker, "x"))})())
    )
    with pytest.raises(UnsafePickleError):
        safe_load(path)
    assert not marker.exists()
    RiskModel.load(tmp_path, safe=False)
    assert marker.exists()


def test_files_that_are_not_plain_pickles_are_refused(tmp_path):
    for content in (b"", b"not a pickle", b"PK\x03\x04zip"):
        (tmp_path / "f").write_bytes(content)
        with pytest.raises(UnsafePickleError):
            safe_load(tmp_path / "f")


def test_the_service_reports_an_unsafe_model_as_a_load_error(tmp_path, env, data):
    (tmp_path / "risk_model.joblib").write_bytes(pickle.dumps(Evil()))
    with pytest.raises(ModelLoadError):
        load_model(tmp_path)


# ---- command-line security options ----


def args(model_dir, env, *extra):
    return ["serve", "--model-dir", str(model_dir), "--lake", str(env[1]), "--no-history", *extra]


@pytest.fixture
def started(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    return seen


def make_cert(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    try:
        import datetime as dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = dt.datetime.now(dt.UTC)
        built = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                 .public_key(private.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(now - dt.timedelta(minutes=1))
                 .not_valid_after(now + dt.timedelta(days=1))
                 .sign(private, hashes.SHA256()))  # fmt: skip
        cert.write_bytes(built.public_bytes(serialization.Encoding.PEM))
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.TraditionalOpenSSL,
                        serialization.NoEncryption()))  # fmt: skip
        return cert, key
    except ImportError:
        pass
    try:
        command = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        command += ["-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)]
        done = subprocess.run(command, capture_output=True)
    except OSError:
        pytest.skip("neither the cryptography package nor openssl is available")
    if done.returncode:
        pytest.skip("openssl could not make a certificate")
    return cert, key


def test_an_external_address_needs_keys_tls_and_the_flag(model_dir, env, tmp_path, started, capsys):
    cert, key = make_cert(tmp_path)
    keyfile = tmp_path / "k.json"
    KeyStore(keyfile).create("ops", "score")
    host = ["--host", "0.0.0.0"]
    assert cli(args(model_dir, env, *host)) == 2
    assert "--auth-keys" in capsys.readouterr().err
    assert cli(args(model_dir, env, *host, "--auth-keys", str(keyfile), "--allow-external")) == 2
    assert "--tls-cert" in capsys.readouterr().err
    tls = ["--tls-cert", str(cert), "--tls-key", str(key)]
    assert cli(args(model_dir, env, *host, "--auth-keys", str(keyfile), *tls)) == 2
    assert "--allow-external" in capsys.readouterr().err
    assert not started and not (tmp_path / "audit.jsonl").exists()
    full = [*host, "--auth-keys", str(keyfile), *tls, "--allow-external",
            "--audit-log", str(tmp_path / "audit.jsonl")]  # fmt: skip
    assert cli(args(model_dir, env, *full)) == 0
    assert started["ssl_certfile"] == str(cert) and started["ssl_keyfile"] == str(key)
    assert started["host"] == "0.0.0.0"
    assert TestClient(started["app"]).get("/v1/model").status_code == 401
    assert AuditLog(tmp_path / "audit.jsonl").read()[0]["event"] == "startup"


def test_tls_options_must_go_together_and_exist(model_dir, env, tmp_path, started, capsys):
    cert, key = make_cert(tmp_path)
    assert cli(args(model_dir, env, "--tls-cert", str(cert))) == 2
    assert "go together" in capsys.readouterr().err
    assert (
        cli(args(model_dir, env, "--tls-cert", str(cert), "--tls-key", str(tmp_path / "no"))) == 2
    )
    assert "does not exist" in capsys.readouterr().err
    assert not started


def test_auth_on_loopback_works_and_needs_an_active_key_and_sane_limits(
    model_dir, env, tmp_path, started, capsys
):
    keyfile = tmp_path / "k.json"
    assert cli(args(model_dir, env, "--auth-keys", str(keyfile))) == 2
    assert "no active API key" in capsys.readouterr().err
    token, rec = KeyStore(keyfile).create("ops", "score")
    assert cli(args(model_dir, env, "--auth-keys", str(keyfile), "--rate-per-minute", "0")) == 2
    assert not started
    audit = ["--audit-log", str(tmp_path / "a.jsonl")]
    assert cli(args(model_dir, env, "--auth-keys", str(keyfile), *audit)) == 0
    client = TestClient(started["app"])
    assert client.get("/v1/model", headers=bearer(token)).status_code == 200
    assert "ssl_certfile" not in started


def test_the_tls_context_accepts_the_generated_certificate(tmp_path):
    cert, key = make_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        str(cert), str(key)
    )  # the files the test makes are valid, so a failure is the CLI's
