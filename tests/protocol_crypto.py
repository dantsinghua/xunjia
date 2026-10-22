"""Independent standard AES-GCM oracle for protocol tests, never production code."""
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def seal(key, plaintext):
    nonce=secrets.token_bytes(12)
    return nonce+AESGCM(bytes(key)).encrypt(nonce,bytes(plaintext),None)

def open_sealed(key, blob):
    try:return AESGCM(bytes(key)).decrypt(bytes(blob[:12]),bytes(blob[12:]),None)
    except Exception:raise ValueError('密封包无法打开') from None
