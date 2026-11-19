"""Cifra do token do bot.


O formato do blob é contrato (v1): `0x01 ‖ key_id(4) ‖ nonce(12) ‖ texto cifrado ‖ tag(16)`, com
`key_id = HKDF-SHA256(ikm, salt=None, info=b"cq/master-key-id/v1", L=4)`, chave
`HKDF-SHA256(ikm, salt=None, info=b"cq/bot-token/aes-256-gcm/v1", L=32)` e dado associado
`b"cq/bot-token/v1\\x00" ‖ blob[0:5] ‖ bot_uuid.bytes ‖ telegram_bot_id (8 bytes, big-endian, com
sinal)`, onde `ikm` é a chave-mestra decodificada (`base64.urlsafe_b64decode(chave + "=")`).

O vetor conhecido é montado aqui de forma **independente**, com os primitivos da `cryptography`
(`HKDF` e `AESGCM`), e fixado também como literal hexadecimal. Fora dele, os testes usam a chave-mestra
da sessão (`tests/helpers/secrets.py`).
"""

from __future__ import annotations

import base64
import traceback
import uuid

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cq_server.secretbox import TokenCipher, UnreadableToken
from tests.helpers.secrets import MASTER_KEY, OTHER_MASTER_KEY

# --- vetor conhecido ------------------------------------------------------------------------------

KAT_IKM = bytes(range(32))
KAT_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
KAT_NONCE = bytes(range(0xA0, 0xAC))
KAT_UUID = uuid.UUID("6f1c2a3b-4d5e-4f60-8a71-92b3c4d5e6f7")
KAT_BOT_ID = 6100000001
KAT_TOKEN = "6100000001:AbCdEfGhIjKlMnOpQrStUvWxYz0123456-_"
KAT_KEY_ID_HEX = "2b8ca833"
KAT_BLOB_HEX = (
    "012b8ca833a0a1a2a3a4a5a6a7a8a9aaab42fac558e1764a5cfbc5e2610ec864f68ce3f624e9b0ae13bb7f04"
    "711ad21b955cd314b512a5ab3bd98df74fb0b17e34e30f517f4bd024407b09d0b6ecae"
)
KAT_BLOB = bytes.fromhex(KAT_BLOB_HEX)

# Regiões do blob do vetor conhecido (token de 46 caracteres → 33 + 46 = 79 bytes).
VERSION_AT = 0
KEY_ID = range(1, 5)
NONCE = range(5, 17)
CIPHERTEXT = range(17, 17 + len(KAT_TOKEN))
TAG = range(17 + len(KAT_TOKEN), 33 + len(KAT_TOKEN))

TOKEN_46 = "6100000002:" + "Zy9_-Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fed"
TOKEN_SHORTEST = "1:" + "a" * 30
TOKEN_80 = "6100000003:" + ("Q1w2E3r4-_" * 7)[:69]
assert (len(KAT_TOKEN), len(TOKEN_SHORTEST), len(TOKEN_46), len(TOKEN_80)) == (46, 32, 46, 80)


def _ikm(master_key: str) -> bytes:
    return base64.urlsafe_b64decode(master_key + "=")


def _hkdf(ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(ikm)


def _key_id(ikm: bytes) -> bytes:
    return _hkdf(ikm, b"cq/master-key-id/v1", 4)


def _aead_key(ikm: bytes) -> bytes:
    return _hkdf(ikm, b"cq/bot-token/aes-256-gcm/v1", 32)


def _aad(header: bytes, bot_uuid: uuid.UUID, telegram_bot_id: int) -> bytes:
    return (b"cq/bot-token/v1\x00" + header + bot_uuid.bytes
            + telegram_bot_id.to_bytes(8, "big", signed=True))


def _independent_encrypt(ikm: bytes, nonce: bytes, token: str, bot_uuid: uuid.UUID,
                         telegram_bot_id: int) -> bytes:
    header = b"\x01" + _key_id(ikm)
    aad = _aad(header, bot_uuid, telegram_bot_id)
    return header + nonce + AESGCM(_aead_key(ikm)).encrypt(nonce, token.encode("ascii"), aad)


def _independent_decrypt(ikm: bytes, blob: bytes, bot_uuid: uuid.UUID, telegram_bot_id: int) -> str:
    aad = _aad(blob[:5], bot_uuid, telegram_bot_id)
    return AESGCM(_aead_key(ikm)).decrypt(blob[5:17], blob[17:], aad).decode("ascii")


def _unreadable(cipher: TokenCipher, blob: bytes, *, bot_uuid: uuid.UUID = KAT_UUID,
                telegram_bot_id: int = KAT_BOT_ID) -> UnreadableToken:
    with pytest.raises(UnreadableToken) as excinfo:
        cipher.decrypt(blob, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id)
    return excinfo.value


def _flip(blob: bytes, index: int) -> bytes:
    changed = bytearray(blob)
    changed[index] ^= 0x01
    return bytes(changed)


def _with_byte(blob: bytes, index: int, value: int) -> bytes:
    changed = bytearray(blob)
    changed[index] = value
    return bytes(changed)


# --- vetor conhecido -------------------------------------------------------------------------------

def test_known_answer_vector_byte_by_byte() -> None:
    """Cifragem autenticada (o formato é contrato), com o
    vetor conhecido: com a chave `bytes(range(32))`, nonce injetado, UUID e id fixos, o blob
    de `encrypt` é igual byte a byte ao montado com `HKDF`/`AESGCM` (e ao literal), o `key_id` é o
    derivado, o `nonce_source` é chamado uma vez com 12, e `decrypt` devolve o token."""
    assert base64.urlsafe_b64encode(KAT_IKM).decode("ascii").rstrip("=") == KAT_KEY
    expected = _independent_encrypt(KAT_IKM, KAT_NONCE, KAT_TOKEN, KAT_UUID, KAT_BOT_ID)
    assert expected == KAT_BLOB
    assert len(KAT_BLOB) == 33 + len(KAT_TOKEN)

    requested: list[int] = []

    def nonce_source(size: int) -> bytes:
        requested.append(size)
        return KAT_NONCE

    cipher = TokenCipher(KAT_KEY, nonce_source=nonce_source)
    assert cipher.key_id == bytes.fromhex(KAT_KEY_ID_HEX)
    assert cipher.key_id == _key_id(KAT_IKM)
    blob = cipher.encrypt(KAT_TOKEN, bot_uuid=KAT_UUID, telegram_bot_id=KAT_BOT_ID)
    assert type(blob) is bytes
    assert blob == KAT_BLOB
    assert requested == [12]
    token = cipher.decrypt(KAT_BLOB, bot_uuid=KAT_UUID, telegram_bot_id=KAT_BOT_ID)
    assert type(token) is str
    assert token == KAT_TOKEN


# --- ida e volta -----------------------------------------------------------------------------------

@pytest.mark.parametrize("token", [TOKEN_SHORTEST, TOKEN_46, TOKEN_80], ids=["32", "46", "80"])
def test_round_trip_with_the_session_key(token: str) -> None:
    """Tamanho 33 + len(token), de 34 a 113 bytes:
    com a chave da sessão, o blob tem a versão 1, o `key_id` derivado e o tamanho exato; decifra
    com a própria cifra, com outra instância da mesma chave e de forma independente; o token não
    aparece em claro no blob."""
    bot_uuid = uuid.uuid4()
    telegram_bot_id = int(token.split(":", 1)[0])
    cipher = TokenCipher(MASTER_KEY)
    blob = cipher.encrypt(token, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id)
    assert len(blob) == 33 + len(token)
    assert blob[0] == 1
    assert blob[1:5] == _key_id(_ikm(MASTER_KEY)) == cipher.key_id
    assert token.encode("ascii") not in blob
    assert token.split(":", 1)[1].encode("ascii") not in blob
    assert cipher.decrypt(blob, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id) == token
    other_instance = TokenCipher(MASTER_KEY)
    assert other_instance.key_id == cipher.key_id
    assert other_instance.decrypt(blob, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id) == token
    assert _independent_decrypt(_ikm(MASTER_KEY), blob, bot_uuid, telegram_bot_id) == token


def test_two_encryptions_of_the_same_token_differ() -> None:
    """Nonce aleatório de 12 bytes (duas cifragens iguais dão blobs
    diferentes): o mesmo token, com o mesmo bot, dá dois blobs com o mesmo cabeçalho e nonces
    diferentes, e os dois decifram para o token."""
    cipher = TokenCipher(MASTER_KEY)
    first = cipher.encrypt(TOKEN_46, bot_uuid=KAT_UUID, telegram_bot_id=6100000002)
    second = cipher.encrypt(TOKEN_46, bot_uuid=KAT_UUID, telegram_bot_id=6100000002)
    assert first != second
    assert first[:5] == second[:5]
    assert first[5:17] != second[5:17]
    for blob in (first, second):
        assert cipher.decrypt(blob, bot_uuid=KAT_UUID, telegram_bot_id=6100000002) == TOKEN_46


# --- adulteração -----------------------------------------------------------------------------------

TAMPER_CASES: list[tuple[str, bytes, str]] = [
    ("versao-0", _with_byte(KAT_BLOB, VERSION_AT, 0x00), "malformed"),
    ("versao-2", _with_byte(KAT_BLOB, VERSION_AT, 0x02), "malformed"),
    ("versao-ff", _with_byte(KAT_BLOB, VERSION_AT, 0xFF), "malformed"),
    *[(f"key-id-{i}", _flip(KAT_BLOB, i), "key_mismatch") for i in KEY_ID],
    *[(f"nonce-{i}", _flip(KAT_BLOB, i), "tampered") for i in NONCE],
    *[(f"cifrado-{i}", _flip(KAT_BLOB, i), "tampered") for i in CIPHERTEXT],
    *[(f"tag-{i}", _flip(KAT_BLOB, i), "tampered") for i in TAG],
    ("truncado-0", KAT_BLOB[:0], "malformed"),
    ("truncado-1", KAT_BLOB[:1], "malformed"),
    ("truncado-5", KAT_BLOB[:5], "malformed"),
    ("truncado-16", KAT_BLOB[:16], "malformed"),
    ("truncado-17", KAT_BLOB[:17], "malformed"),
    ("truncado-33", KAT_BLOB[:33], "malformed"),
    ("truncado-34", KAT_BLOB[:34], "tampered"),
    ("sem-o-ultimo-byte", KAT_BLOB[:-1], "tampered"),
    ("byte-a-mais", KAT_BLOB + b"\x00", "tampered"),
]


@pytest.mark.parametrize(("blob", "reason"), [(b, r) for _, b, r in TAMPER_CASES],
                         ids=[i for i, _, _ in TAMPER_CASES])
def test_tampering_matrix(blob: bytes, reason: str) -> None:
    """Token ilegível → sem acesso, com o motivo (`malformed`: menos de
    34 bytes ou versão ≠ 1; `key_mismatch`: `key_id` diferente; `tampered`: tag inválida);
    adulteração por região: versão → `malformed`; cada byte do `key_id` →
    `key_mismatch`; cada byte do nonce, do texto cifrado e da tag → `tampered`; truncar para 0, 16 e
    33 → `malformed` (34 já é `tampered`); `str` do erro é só o motivo."""
    exc = _unreadable(TokenCipher(KAT_KEY), blob)
    assert exc.reason == reason
    assert str(exc) == reason


@pytest.mark.parametrize(("bot_uuid", "telegram_bot_id"), [
    (uuid.UUID("6f1c2a3b-4d5e-4f60-8a71-92b3c4d5e6f8"), KAT_BOT_ID),
    (uuid.UUID(int=0), KAT_BOT_ID),
    (KAT_UUID, KAT_BOT_ID + 1),
    (KAT_UUID, -KAT_BOT_ID),
    (KAT_UUID, 0),
], ids=["outro-uuid", "uuid-zero", "id-mais-um", "id-negativo", "id-zero"])
def test_other_bot_uuid_or_id_is_tampered(bot_uuid: uuid.UUID, telegram_bot_id: int) -> None:
    """O dado associado amarra o token ao bot (UUID
    trocado ou id + 1 → `tampered`): decifrar o blob de um bot como se fosse de outro →
    `tampered`; com o bot certo, decifra."""
    cipher = TokenCipher(KAT_KEY)
    exc = _unreadable(cipher, KAT_BLOB, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id)
    assert exc.reason == "tampered"
    assert cipher.decrypt(KAT_BLOB, bot_uuid=KAT_UUID, telegram_bot_id=KAT_BOT_ID) == KAT_TOKEN


def test_other_master_key_is_key_mismatch() -> None:
    """Token que não pode ser decifrado com a chave atual
    (`key_mismatch`; a chave é válida, mas é outra): o blob de uma chave, lido com outra
    chave válida, → `key_mismatch`, nos dois sentidos."""
    exc = _unreadable(TokenCipher(MASTER_KEY), KAT_BLOB)
    assert exc.reason == "key_mismatch"
    blob = TokenCipher(MASTER_KEY).encrypt(TOKEN_46, bot_uuid=KAT_UUID, telegram_bot_id=6100000002)
    exc = _unreadable(TokenCipher(OTHER_MASTER_KEY), blob, telegram_bot_id=6100000002)
    assert exc.reason == "key_mismatch"
    assert TokenCipher(MASTER_KEY).key_id != TokenCipher(OTHER_MASTER_KEY).key_id


# --- `repr` e `str` --------------------------------------------------------------------------------

def test_repr_and_str_have_neither_key_nor_token() -> None:
    """O token não aparece em mensagens de erro nem em logs (`repr` sem a
    chave; `str(UnreadableToken)` é só o motivo): `repr`/`str` da cifra não têm a
    chave-mestra, os bytes dela nem a chave derivada; `str`, `repr`, `args` e o traceback formatado
    de cada `UnreadableToken` não têm o token, a chave-mestra nem a chave derivada."""
    derived_hex = _aead_key(_ikm(MASTER_KEY)).hex()
    forbidden = [MASTER_KEY, MASTER_KEY[:12], _ikm(MASTER_KEY).hex(), repr(_ikm(MASTER_KEY)),
                 derived_hex, TOKEN_46, TOKEN_46.split(":", 1)[1], TOKEN_46.split(":", 1)[1][:12]]
    cipher = TokenCipher(MASTER_KEY)
    for text in (repr(cipher), str(cipher)):
        for value in forbidden:
            assert value not in text
    blob = cipher.encrypt(TOKEN_46, bot_uuid=KAT_UUID, telegram_bot_id=6100000002)
    cases = [
        (_with_byte(blob, 0, 2), 6100000002, "malformed"),
        (blob, 6100000002, "key_mismatch"),
        (blob, 6100000003, "tampered"),
    ]
    for bad_blob, telegram_bot_id, reason in cases:
        reader = TokenCipher(OTHER_MASTER_KEY) if reason == "key_mismatch" else cipher
        exc = _unreadable(reader, bad_blob, telegram_bot_id=telegram_bot_id)
        assert str(exc) == reason
        rendered = [str(exc), repr(exc), *map(str, exc.args),
                    "".join(traceback.format_exception(exc))]
        for text in rendered:
            for value in [*forbidden, OTHER_MASTER_KEY, OTHER_MASTER_KEY[:12]]:
                assert value not in text
