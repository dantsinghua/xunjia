"""Ephemeral synthetic client certificate for offline configuration contract tests."""
from functools import lru_cache
import atexit
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

@lru_cache(maxsize=1)
def tls_config():
    temporary=tempfile.TemporaryDirectory(prefix="synthetic-relay-pki-")
    atexit.register(temporary.cleanup)
    root=Path(temporary.name)
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"synthetic-tests-only")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False)
        .sign(key,hashes.SHA256()))
    cp=root/'client.crt';kp=root/'client.key'
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));kp.chmod(0o600)
    return {"client_cert_file":str(cp),"client_key_file":str(kp),"ca_file":str(cp)}
