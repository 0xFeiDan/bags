import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptionNotConfigured(RuntimeError):
    pass


class CredentialCipher:
    """AES-256-GCM envelope used only for connector credentials at rest."""

    def __init__(self, encoded_key: str | None) -> None:
        if not encoded_key:
            raise EncryptionNotConfigured("MASTER_ENCRYPTION_KEY is not configured")
        try:
            key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
        except (ValueError, binascii.Error) as error:
            raise EncryptionNotConfigured("MASTER_ENCRYPTION_KEY must be base64url encoded") from error
        if len(key) != 32:
            raise EncryptionNotConfigured("MASTER_ENCRYPTION_KEY must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, envelope: str) -> str:
        packed = base64.urlsafe_b64decode(envelope.encode("ascii"))
        return self._cipher.decrypt(packed[:12], packed[12:], None).decode("utf-8")
