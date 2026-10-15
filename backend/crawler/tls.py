"""Verified TLS using the operating system's certificate trust store."""

import os
import ssl
import truststore


def tls_context():
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cafile = os.getenv("SSL_CERT_FILE")
    capath = os.getenv("SSL_CERT_DIR")
    if cafile or capath:
        context.load_verify_locations(cafile=cafile, capath=capath)
    return context
