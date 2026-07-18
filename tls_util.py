"""Auto TLS certificate helpers for local / LAN HTTPS."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
CERT_DIR_NAME = "certs"
CERT_BASENAME = "web-scrcpy"
META_FILENAME = "meta.json"
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$",
    re.IGNORECASE,
)


def ensure_cryptography_package() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("cryptography not found, installing…")
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "cryptography",
                "-i",
                PYPI_MIRROR,
            ]
        )
        import cryptography  # noqa: F401


def normalize_tls_domains(domains: Iterable[str] | None) -> list[str]:
    """Normalize and dedupe user-supplied hostnames for certificate SAN."""
    if not domains:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        if "://" in value:
            value = value.split("://", 1)[1]
        if "/" in value:
            value = value.split("/", 1)[0]
        if ":" in value and value.count(":") == 1:
            host, port = value.rsplit(":", 1)
            if port.isdigit():
                value = host
        if value in seen:
            continue
        if value in {"localhost", "web-scrcpy.local"}:
            seen.add(value)
            out.append(value)
            continue
        if not _HOSTNAME_RE.match(value):
            raise ValueError(f"invalid tls domain: {raw!r}")
        seen.add(value)
        out.append(value)
    return out


def get_local_ipv4_addresses() -> list[str]:
    """Collect likely LAN IPv4 addresses for certificate SAN entries."""
    ips: set[str] = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(ips)


def _cert_paths(base_dir: Path) -> tuple[Path, Path, Path]:
    cert_dir = base_dir / CERT_DIR_NAME
    cert_dir.mkdir(parents=True, exist_ok=True)
    return (
        cert_dir / f"{CERT_BASENAME}.crt",
        cert_dir / f"{CERT_BASENAME}.key",
        cert_dir / META_FILENAME,
    )


def _load_meta(meta_path: Path) -> dict | None:
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cert_needs_refresh(
    cert_path: Path,
    key_path: Path,
    meta_path: Path,
    local_ips: Iterable[str],
    domains: Iterable[str],
) -> bool:
    if not cert_path.is_file() or not key_path.is_file():
        return True

    meta = _load_meta(meta_path)
    if meta is None:
        return True

    saved_ips = sorted(set(meta.get("ips") or []))
    if saved_ips != sorted(set(local_ips)):
        return True

    saved_domains = sorted(set(meta.get("domains") or []))
    if saved_domains != sorted(set(domains)):
        return True

    not_after = meta.get("not_after")
    if not not_after:
        return True
    try:
        expires = datetime.fromisoformat(not_after)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc) + timedelta(days=14):
            return True
    except ValueError:
        return True

    return False


def _write_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    local_ips: list[str],
    domains: list[str],
) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    common_name = domains[0] if domains else "web-scrcpy.local"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Web-scrcpy"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )

    now = datetime.now(timezone.utc)
    not_after = now + timedelta(days=365)
    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName("web-scrcpy.local"),
    ]
    for domain in domains:
        if domain not in {"localhost", "web-scrcpy.local"}:
            san.append(x509.DNSName(domain))
    for ip in local_ips:
        try:
            san.append(x509.IPAddress(ipaddress_from_str(ip)))
        except ValueError:
            continue

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return {
        "ips": local_ips,
        "domains": domains,
        "not_after": not_after.isoformat(),
        "generated_at": now.isoformat(),
    }


def ipaddress_from_str(value: str):
    import ipaddress

    return ipaddress.ip_address(value)


def ensure_tls_material(
    base_dir: Path,
    tls_domains: list[str] | None = None,
) -> tuple[Path, Path, list[str], list[str]]:
    """
    Ensure a usable self-signed certificate exists for localhost + LAN IPs + domains.
    Installs cryptography via pip when missing.
    """
    ensure_cryptography_package()
    local_ips = get_local_ipv4_addresses()
    domains = normalize_tls_domains(tls_domains)
    cert_path, key_path, meta_path = _cert_paths(base_dir)

    if _cert_needs_refresh(cert_path, key_path, meta_path, local_ips, domains):
        print("Generating self-signed TLS certificate for HTTPS…")
        print("  SAN IPs:", ", ".join(local_ips))
        if domains:
            print("  SAN domains:", ", ".join(domains))
        meta = _write_self_signed_cert(cert_path, key_path, local_ips, domains)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"  cert: {cert_path}")
        print(f"  key:  {key_path}")
        print("  Note: browsers will warn about self-signed cert — choose 'Continue' to proceed.")
        if domains:
            print(
                "  If the domain previously used HTTPS (HSTS), clear it in the browser "
                "or use a trusted certificate (e.g. Let's Encrypt)."
            )
    else:
        print(f"Using existing TLS certificate: {cert_path}")

    return cert_path, key_path, local_ips, domains


def resolve_ssl_paths(
    base_dir: Path,
    use_https: bool,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    tls_domains: list[str] | None = None,
) -> tuple[str | None, str | None, list[str], list[str]]:
    domains = normalize_tls_domains(tls_domains)
    if ssl_certfile and ssl_keyfile:
        cert = Path(ssl_certfile)
        key = Path(ssl_keyfile)
        if not cert.is_file() or not key.is_file():
            raise FileNotFoundError(f"TLS file not found: cert={cert} key={key}")
        return str(cert), str(key), get_local_ipv4_addresses(), domains

    if not use_https:
        return None, None, get_local_ipv4_addresses(), domains

    cert_path, key_path, local_ips, domains = ensure_tls_material(base_dir, domains)
    return str(cert_path), str(key_path), local_ips, domains
