"""
crypto.py — шифрование/дешифрование чувствительных данных (Fernet/AES).

Переменная среды:
    ENCRYPTION_KEY — base64url-encoded 32-байтный ключ Fernet.
                     Генерация: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Функции:
    encrypt_token(plaintext: str) -> str   — шифрует, возвращает base64-строку
    decrypt_token(ciphertext: str) -> str  — дешифрует, возвращает исходный токен

ВАЖНО:
    - Ключ никогда не логируется и не попадает в исключения.
    - Если ключ не задан — поднимается RuntimeError, а не тихий fallback.
    - При невозможности дешифровать — поднимается CryptoDecryptError
      (например, если токен зашифрован другим ключом или повреждён).
"""

import base64
import binascii
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_ENV = "ENCRYPTION_KEY"
_fernet: Fernet | None = None


class CryptoError(Exception):
    """Базовый класс для ошибок шифрования."""


class CryptoDecryptError(CryptoError):
    """Не удалось расшифровать — ключ не совпадает или данные повреждены."""


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        raw_key = os.environ.get(_ENCRYPTION_KEY_ENV, "").strip()
        if not raw_key:
            raise RuntimeError(
                f"Переменная среды {_ENCRYPTION_KEY_ENV!r} не задана. "
                "Сгенерируй ключ: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            _fernet = Fernet(raw_key.encode())
        except (TypeError, ValueError):
            raise RuntimeError(f"Неверный формат ключа {_ENCRYPTION_KEY_ENV}") from None
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Зашифровать токен. Возвращает base64-строку (безопасно для хранения в TEXT-колонке)."""
    if not plaintext:
        raise CryptoError("Нечего шифровать: пустой токен")
    f = _get_fernet()
    encrypted_bytes = f.encrypt(plaintext.encode("utf-8"))
    return encrypted_bytes.decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    """Расшифровать токен. Принимает строку, возвращает исходный токен.

    Raises:
        CryptoDecryptError — если расшифровка не удалась.
    """
    if not isinstance(ciphertext, str) or not ciphertext:
        raise CryptoDecryptError("Отсутствует шифротекст")
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        # НЕ логируем ciphertext — он может содержать чувствительные данные
        logger.warning("decrypt_token: не удалось расшифровать токен (%s)", type(exc).__name__)
        raise CryptoDecryptError(
            "Не удалось расшифровать токен. Возможно, изменился ключ шифрования — "
            "пересохрани токен через /setup_mymeet."
        ) from exc


def is_encrypted(value: str) -> bool:
    """Recognize the legacy Fernet wire format; decrypt_token authenticates it."""
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    # Version byte, timestamp, IV and authentication tag total 57 bytes.
    # The AES-CBC ciphertext contains at least one padded 16-byte block.
    return (len(raw) >= 73 and raw[0] == 0x80 and (len(raw) - 57) % 16 == 0
            and base64.urlsafe_b64encode(raw).decode("ascii") == value)
