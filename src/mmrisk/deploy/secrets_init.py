"""Make the secrets a deployment needs: API keys, a TLS certificate, two passwords.

Everything goes under one folder (default `secrets/`, ignored by Git and Docker). Keys and the
dashboard password are printed once and not stored in plain text; the metrics key must be a file
(Prometheus reads it from one) and so must the Grafana password: those two are stored as files
with owner-only permissions where the system has them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from mmrisk.deploy import DeployError
from mmrisk.security import passwords
from mmrisk.security.keys import KeyStore

API_KEYS = "api_keys.json"
METRICS_KEY = "metrics_key"
CERT = "tls/cert.pem"
KEY = "tls/key.pem"
DASH_HASH = "dashboard.hash"
GRAFANA = "grafana_admin_password"
FILES = (API_KEYS, METRICS_KEY, CERT, KEY, DASH_HASH, GRAFANA)
CERT_NAMES = (
    "localhost",
    "api",
)  # the names clients use: the host's loopback and the compose service
CERT_DAYS = 365


@dataclass(frozen=True)
class Created:
    """Secrets that exist only in this object: show them to the person and let them go."""

    score_key: str
    admin_key: str
    dashboard_password: str
    cert_expires: str


def _private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def make_certificate(cert_path: Path, key_path: Path, days: int = CERT_DAYS) -> str:
    """A self-signed certificate for localhost, api and 127.0.0.1 (it is its own trust anchor)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise DeployError(
            'the "cryptography" package is needed to make a certificate: pip install -e ".[deploy]"'
        ) from None
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mmrisk local")])
    now = dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(minutes=5)
    end = now + dt.timedelta(days=days)
    if end <= start:  # only for tests that need an already-expired certificate
        start = end - dt.timedelta(days=1)
    san = [x509.DNSName(n) for n in CERT_NAMES]
    san += [x509.IPAddress(ipaddress.ip_address(a)) for a in ("127.0.0.1", "::1")]
    ski = x509.SubjectKeyIdentifier.from_public_key(private.public_key())
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          crl_sign=False, encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(private.public_key()),
                       critical=False)
        .sign(private, hashes.SHA256())
    )  # fmt: skip
    _private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    _private(
        key_path,
        private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )  # fmt: skip
    return (now + dt.timedelta(days=days)).date().isoformat()


def init(secrets_dir: Path, force: bool = False) -> Created:
    root = Path(secrets_dir)
    present = [f for f in FILES if (root / f).exists()]
    if present and not force:
        raise DeployError(
            f"{root} already holds {', '.join(present)}; --force replaces them (every old key and "
            "password stops working)"
        )
    if present:
        for f in FILES:
            (root / f).unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "tls").mkdir(exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    store = KeyStore(root / API_KEYS)
    score, _ = store.create("application", "score")
    metrics, _ = store.create("prometheus", "metrics")
    admin, _ = store.create("operator", "admin")
    _private(root / METRICS_KEY, (metrics + "\n").encode())
    with contextlib.suppress(OSError):
        os.chmod(root / API_KEYS, 0o600)
    dashboard = secrets.token_urlsafe(18)
    _private(root / DASH_HASH, (passwords.hash_password(dashboard) + "\n").encode())
    _private(root / GRAFANA, (secrets.token_urlsafe(18) + "\n").encode())
    expires = make_certificate(root / CERT, root / KEY)
    return Created(score, admin, dashboard, expires)


def remove(secrets_dir: Path) -> None:
    """Delete the whole folder (used by tests and by a teardown that wants no trace)."""
    shutil.rmtree(secrets_dir, ignore_errors=True)
