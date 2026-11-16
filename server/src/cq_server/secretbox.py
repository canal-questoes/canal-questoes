"""Cifra do token do bot.

AES-256-GCM com a chave derivada da chave-mestra do ambiente (`CQ_MASTER_KEY`) por HKDF-SHA256. O
formato do blob é contrato (v1): `0x01 ‖ key_id(4) ‖ nonce(12) ‖ texto cifrado ‖ tag(16)`, com o
dado associado amarrando o token ao bot (`bot_uuid` e `telegram_bot_id`). O `repr` nunca mostra a
chave, e o `str(UnreadableToken)` é só o motivo.

- **Chaves**: a entrada do HKDF é a chave-mestra **decodificada** (32
  bytes, na forma canônica de `config.master_key_bytes`); a chave da cifra é
  `HKDF-SHA256(ikm, salt=None, info=KEY_INFO, L=32)` e o `key_id`,
  `HKDF-SHA256(ikm, salt=None, info=KEY_ID_INFO, L=4)`. A instância guarda só as duas derivadas,
  nunca a chave-mestra.
- **Dado associado:** `AAD_PREFIX ‖ blob[0:5] ‖ bot_uuid (16 bytes) ‖ telegram_bot_id (8 bytes,
  big-endian, com sinal)`: autenticar o cabeçalho impede trocá-lo sem ser detectado, e amarrar ao
  bot impede trocar blobs entre bots.
- **Leitura:** menos de 34 bytes ou versão ≠ 1 → `malformed`; `key_id` diferente → `key_mismatch`,
  sem tentar decifrar; tag inválida → `tampered`. O erro sai sem a exceção da biblioteca encadeada.
- Um `AESGCM` novo por operação (só a `Fernet` tem garantia documentada entre threads).
"""

from __future__ import annotations

import hmac
import os
import uuid
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cq_server.config import master_key_bytes

# Formato v1 do blob e rótulos do HKDF e do dado associado.
FORMAT_VERSION = 1
KEY_ID_LENGTH = 4
NONCE_LENGTH = 12
KEY_LENGTH = 32
TAG_LENGTH = 16
HEADER_LENGTH = 1 + KEY_ID_LENGTH
# Cabeçalho, nonce e tag, mais pelo menos 1 byte de texto cifrado (34 bytes).
MIN_BLOB_LENGTH = HEADER_LENGTH + NONCE_LENGTH + TAG_LENGTH + 1
KEY_INFO = b"cq/bot-token/aes-256-gcm/v1"
KEY_ID_INFO = b"cq/master-key-id/v1"
AAD_PREFIX = b"cq/bot-token/v1\x00"

# Motivos de `UnreadableToken`.
UNREADABLE_REASONS = ("malformed", "key_mismatch", "tampered")


class UnreadableToken(Exception):
    """O blob não pode ser decifrado; `reason` em `UNREADABLE_REASONS`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _hkdf(ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(ikm)


def _aad(header: bytes, bot_uuid: uuid.UUID, telegram_bot_id: int) -> bytes:
    return AAD_PREFIX + header + bot_uuid.bytes + telegram_bot_id.to_bytes(8, "big", signed=True)


class TokenCipher:
    """Cifra e decifra tokens de bot com a chave-mestra (formato v1)."""

    def __init__(self, master_key: str, *,
                 nonce_source: Callable[[int], bytes] = os.urandom) -> None:
        ikm = master_key_bytes(master_key)
        if ikm is None:
            # Sem o valor: a mesma regra de `CQ_MASTER_KEY` (config.BotSettings).
            raise ValueError("chave-mestra malformada")
        self._key = _hkdf(ikm, KEY_INFO, KEY_LENGTH)
        self._key_id = _hkdf(ikm, KEY_ID_INFO, KEY_ID_LENGTH)
        self._nonce_source = nonce_source

    @property
    def key_id(self) -> bytes:
        return self._key_id

    def encrypt(self, token: str, *, bot_uuid: uuid.UUID, telegram_bot_id: int) -> bytes:
        """O blob v1 do token, amarrado ao bot (`bot_uuid`, `telegram_bot_id`)."""
        if not isinstance(token, str) or not token.isascii():
            # Sem o valor na mensagem (e sem o `UnicodeEncodeError`, que guarda o texto).
            raise ValueError("o token do bot deve ser texto ASCII")
        nonce = self._nonce_source(NONCE_LENGTH)
        if not isinstance(nonce, bytes) or len(nonce) != NONCE_LENGTH:
            raise ValueError("o nonce deve ter 12 bytes")
        header = bytes([FORMAT_VERSION]) + self._key_id
        aad = _aad(header, bot_uuid, telegram_bot_id)
        return header + nonce + AESGCM(self._key).encrypt(nonce, token.encode("ascii"), aad)

    def decrypt(self, blob: bytes, *, bot_uuid: uuid.UUID, telegram_bot_id: int) -> str:
        """O token do blob; `UnreadableToken` com `malformed`, `key_mismatch` ou `tampered`."""
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            raise UnreadableToken("malformed")
        data = bytes(blob)
        if len(data) < MIN_BLOB_LENGTH or data[0] != FORMAT_VERSION:
            raise UnreadableToken("malformed")
        header = data[:HEADER_LENGTH]
        if not hmac.compare_digest(header[1:], self._key_id):
            raise UnreadableToken("key_mismatch")
        nonce = data[HEADER_LENGTH:HEADER_LENGTH + NONCE_LENGTH]
        sealed = data[HEADER_LENGTH + NONCE_LENGTH:]
        try:
            plain = AESGCM(self._key).decrypt(nonce, sealed, _aad(header, bot_uuid, telegram_bot_id))
        except InvalidTag:
            raise UnreadableToken("tampered") from None
        try:
            return plain.decode("ascii")
        except UnicodeDecodeError:  # pragma: no cover - só um blob autêntico chega aqui
            raise UnreadableToken("tampered") from None

    def __repr__(self) -> str:
        return "TokenCipher(master_key=<oculta>)"
