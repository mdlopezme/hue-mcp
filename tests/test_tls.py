import hashlib
import re
import ssl
from importlib.resources import files

from hue_mcp.bridge import bridge_ssl_context

SIGNIFY_ROOT_FINGERPRINTS = {
    "f0bd8e6509e82f774d63bc009d5388c969fe3dcf7d6d541d6351b72b898d8acf",  # CN=root-bridge
    "d8b89448b2af8e1676185ac07219ee9dcbc8f01c122a026a2a4b7b5cfe0328b8",  # CN=Hue Root CA 01
}


def test_bundled_roots_are_exactly_signifys():
    pem = (files("hue_mcp") / "hue_roots.pem").read_text()
    certificates = re.findall(
        r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.DOTALL
    )
    fingerprints = {
        hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
        for certificate in certificates
    }
    assert fingerprints == SIGNIFY_ROOT_FINGERPRINTS


def test_bridge_connections_require_a_verified_certificate_naming_the_bridge():
    context = bridge_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert len(context.get_ca_certs()) == 2


def test_pairing_still_verifies_the_chain_before_it_knows_the_bridge_id():
    context = bridge_ssl_context(check_hostname=False)
    assert context.verify_mode == ssl.CERT_REQUIRED
