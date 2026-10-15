import ssl
import pytest
from crawler.tls import tls_context


def test_native_trust_keeps_certificate_and_hostname_verification(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_invalid_explicit_ca_bundle_fails_closed(monkeypatch, tmp_path):
    broken = tmp_path / "invalid-ca.pem"
    broken.write_text("this is not a certificate")
    monkeypatch.setenv("SSL_CERT_FILE", str(broken))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    with pytest.raises(ssl.SSLError):
        tls_context()
