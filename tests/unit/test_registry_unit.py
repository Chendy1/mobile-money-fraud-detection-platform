"""The model registry: versions, the production pointer, rollback, checksums."""

import json

import pytest

from mmrisk.registry import Registry, RegistryError, resolve_model_dir
from mmrisk.registry import store as store_mod
from mmrisk.registry.cli import main


def make_model(path, payload=b"model-bytes", feature_version="1.0"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "risk_model.joblib").write_bytes(payload)
    (path / "risk_model.json").write_text(json.dumps({"feature_version": feature_version}))
    return path


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "reg")


@pytest.fixture
def src(tmp_path):
    return make_model(tmp_path / "src")


def test_register_numbers_versions_and_copies_files(reg, src):
    assert reg.register(src, source="test") == "v0001"
    assert reg.register(src, source="test") == "v0002"
    assert reg.ids() == ["v0001", "v0002"]
    info = reg.get("v0001")
    assert info["stage"] == "candidate" and info["feature_version"] == "1.0"
    assert len(info["sha256"]) == 64
    assert (reg.versions_dir / "v0001" / "risk_model.joblib").read_bytes() == b"model-bytes"
    assert not [p for p in reg.versions_dir.iterdir() if p.name.startswith(".")]


def test_register_rejects_bad_input(reg, tmp_path, src):
    with pytest.raises(RegistryError, match="stage"):
        reg.register(src, source="t", stage="live")
    with pytest.raises(RegistryError, match="not a saved model"):
        reg.register(tmp_path / "empty", source="t")
    with pytest.raises(RegistryError, match="no version v0009"):
        reg.register(src, source="t", parent="v0009")
    assert reg.ids() == []


def test_register_skips_an_id_taken_meanwhile(reg, src, monkeypatch):
    reg.register(src, source="t")
    real = reg._next_id
    calls = iter(["v0001"])
    monkeypatch.setattr(reg, "_next_id", lambda: next(calls, None) or real())
    assert reg.register(src, source="t") == "v0002"
    assert reg.get("v0001")["version"] == "v0001"


def test_promote_needs_approval_a_reason_and_a_real_change(reg, src):
    v = reg.register(src, source="t")
    with pytest.raises(RegistryError, match="not 'approved'"):
        reg.promote(v, "because")
    reg.set_stage(v, "approved")
    with pytest.raises(RegistryError, match="reason"):
        reg.promote(v, "  ")
    pointer = reg.promote(v, "gates passed")
    assert pointer["version"] == v and pointer["previous"] is None and not pointer["forced"]
    with pytest.raises(RegistryError, match="already"):
        reg.promote(v, "again")


def test_quick_versions_need_force(reg, src):
    v = reg.register(src, source="t", stage="approved", quick=True)
    with pytest.raises(RegistryError, match="quick"):
        reg.promote(v, "smoke")
    assert reg.promote(v, "override on purpose", force=True)["forced"] is True


def test_force_promotes_a_rejected_version_and_says_so(reg, src):
    v = reg.register(src, source="t", stage="rejected")
    assert reg.promote(v, "accepting the risk", force=True)["forced"]
    assert reg.history()[-1]["forced"] is True


def test_rollback_returns_to_the_previous_and_flips_back(reg, src):
    a = reg.register(src, source="t", stage="approved")
    b = reg.register(src, source="t", stage="approved")
    with pytest.raises(RegistryError, match="no earlier"):
        reg.rollback("nothing yet")
    reg.promote(a, "first")
    with pytest.raises(RegistryError, match="no earlier"):
        reg.rollback("still nothing")
    reg.promote(b, "second")
    assert reg.rollback("b is bad")["version"] == a
    assert reg.production()["version"] == a and reg.production()["previous"] == b
    assert reg.rollback("changed my mind")["version"] == b
    with pytest.raises(RegistryError, match="reason"):
        reg.rollback("")


def test_history_is_append_only_and_complete(reg, src):
    a = reg.register(src, source="t", stage="approved")
    reg.promote(a, "first")
    reg.set_stage(a, "rejected", note="found a problem")
    actions = [e["action"] for e in reg.history()]
    assert actions == ["register", "promote", "stage"]
    assert reg.history()[2]["before"] == "approved" and reg.history()[2]["after"] == "rejected"
    assert all("at" in e for e in reg.history())


def test_checksum_catches_a_changed_file(reg, src):
    a = reg.register(src, source="t", stage="approved")
    reg.promote(a, "first")
    reg.verify(a)
    (reg.versions_dir / a / "risk_model.joblib").write_bytes(b"tampered")
    with pytest.raises(RegistryError, match="checksum"):
        reg.verify(a)
    with pytest.raises(RegistryError, match="checksum"):
        reg.production_dir()
    with pytest.raises(RegistryError, match="checksum"):
        resolve_model_dir(reg.root)


def test_promote_and_rollback_refuse_a_corrupt_target(reg, src):
    a = reg.register(src, source="t", stage="approved")
    b = reg.register(src, source="t", stage="approved")
    reg.promote(a, "first")
    reg.promote(b, "second")
    (reg.versions_dir / a / "risk_model.joblib").write_bytes(b"x")
    with pytest.raises(RegistryError, match="checksum"):
        reg.rollback("back")
    assert reg.production()["version"] == b


def test_resolve_model_dir(reg, src, tmp_path):
    assert resolve_model_dir(src) == src  # a plain model folder is returned as it is
    a = reg.register(src, source="t", stage="approved")
    with pytest.raises(RegistryError, match="no production"):
        resolve_model_dir(reg.root)  # versions but nothing promoted
    reg.promote(a, "first")
    assert resolve_model_dir(reg.root) == reg.versions_dir / a


def test_production_pointer_that_is_not_json(reg, src):
    reg.register(src, source="t")
    (reg.root / "production.json").write_text("{not json")
    with pytest.raises(RegistryError, match="not valid JSON"):
        reg.production()


def test_pointer_write_is_atomic(reg, src, monkeypatch):
    a = reg.register(src, source="t", stage="approved")
    b = reg.register(src, source="t", stage="approved")
    reg.promote(a, "first")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        reg.promote(b, "second")
    monkeypatch.undo()
    assert reg.production()["version"] == a  # the old pointer is intact


def test_cli_flow(tmp_path, src, capsys):
    root = str(tmp_path / "reg")
    assert main(["--registry", root, "list"]) == 0
    assert "no versions" in capsys.readouterr().out
    assert main(["--registry", root, "bootstrap", "--model-dir", str(src)]) == 0
    assert main(["--registry", root, "bootstrap", "--model-dir", str(src)]) == 2  # only once
    assert "already has a production" in capsys.readouterr().err
    assert main(["--registry", root, "list"]) == 0
    assert "<- production" in capsys.readouterr().out
    assert main(["--registry", root, "promote", "v0001", "--reason", "x"]) == 2
    assert main(["--registry", root, "rollback", "--reason", "x"]) == 2
    assert main(["--registry", root, "verify"]) == 0
    assert main(["--registry", root, "show", "v0001"]) == 0
    assert main(["--registry", root, "show", "v0042"]) == 2
