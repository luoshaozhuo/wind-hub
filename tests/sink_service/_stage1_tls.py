"""Ephemeral CA and mTLS certificates for Sink Service stage-one tests."""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_VALIDITY = datetime.timedelta(days=1)


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _save_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def write_mtls_bundle(root: Path) -> dict[str, Path]:
    """Create CA, server and three client certs under root/tls."""
    tls = root / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC)
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wind-hub test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _VALIDITY)
        .not_valid_after(now + _VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    (tls / "ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    _save_key(tls / "ca.key", ca_key)

    def cert(
        name: str,
        *,
        uri_san: str | None = None,
        dns: tuple[str, ...] = (),
        ips: tuple[str, ...] = (),
    ) -> tuple[Path, Path]:
        key = _key()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _VALIDITY)
            .not_valid_after(now + _VALIDITY)
        )
        alt: list[x509.GeneralName] = [x509.DNSName(value) for value in dns]
        alt.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in ips)
        if uri_san is not None:
            alt.append(x509.UniformResourceIdentifier(uri_san))
        if alt:
            builder = builder.add_extension(x509.SubjectAlternativeName(alt), critical=False)
        signed = builder.sign(ca_key, hashes.SHA256())
        cert_path = tls / f"{name}.crt"
        key_path = tls / f"{name}.key"
        cert_path.write_bytes(signed.public_bytes(serialization.Encoding.PEM))
        _save_key(key_path, key)
        return cert_path, key_path

    server_crt, server_key = cert("sink", dns=("localhost",), ips=("127.0.0.1",))
    admin_crt, admin_key = cert("admin", uri_san="spiffe://wind-hub/maintenance")
    readonly_crt, readonly_key = cert("readonly", uri_san="spiffe://wind-hub/readonly")
    outsider_crt, outsider_key = cert("outsider", uri_san="spiffe://wind-hub/outsider")

    return {
        "ca": tls / "ca.crt",
        "server_crt": server_crt,
        "server_key": server_key,
        "admin_crt": admin_crt,
        "admin_key": admin_key,
        "readonly_crt": readonly_crt,
        "readonly_key": readonly_key,
        "outsider_crt": outsider_crt,
        "outsider_key": outsider_key,
    }
