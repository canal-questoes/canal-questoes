"""Identidades canário e detector independente da implementação (a identidade só sai pela revelação).

O detector **não** usa `cq_server.redaction`: se a redação estiver errada, o canário continua capaz
de achar o vazamento. Ele é agressivo de propósito: procura o nome completo, cada palavra do nome
com 5 letras ou mais, o RA, o e-mail e a parte local do e-mail, em qualquer caixa e acentuação, e
também dentro de JSON escapado e de percent-encoding.

**Telegram:** remetentes canário do Telegram
(`CanaryTelegramUser`: `TG_TEODORICO`, `TG_FILISBINA`, `TG_ZEFIRO`, ids de 13 dígitos na faixa
`73…`), com os alvos `tg-nome:` (nome, sobrenome e os dois juntos), `tg-palavra:` (cada palavra de 5
letras ou mais desses nomes), `tg-username:` (acha também o `@username`) e `tg-id:`. Esses alvos só
entram com `find_canary(texto, prefixes=TG_PREFIXES)` ou `prefixes=ALL_PREFIXES`; **sem**
`prefixes`, os alvos são exatamente os de base (`TARGETS`, os mesmos de `prefixes=BASE_PREFIXES`). Com
`prefixes`, valem os alvos (os de base e os do Telegram) cujo rótulo tem um dos prefixos dados; cada alvo
pertence a um único prefixo.

**Alunos:** dois alunos canário (`CanaryStudent`: `STUDENT_A`, com
2 contas do Telegram, e `STUDENT_B`, com 1), com nome, RA e e-mail da lista da turma (maiúsculas na
parte local e no domínio, em `example.*`) e contas **próprias** (`TG_STUDENT_A1`, `TG_STUDENT_A2`,
`TG_STUDENT_B`: ids de 13 dígitos na faixa `75…`, que não contêm nem estão contidos nos ids e RAs
canário de base e do Telegram, e nome, sobrenome e `@username` inconfundíveis). Os alvos têm os prefixos `aluno-*`
de `STUDENT_PREFIXES`, fora de `ALL_PREFIXES`: `aluno-nome:`, `aluno-palavra:`, `aluno-ra:`,
`aluno-email:`, `aluno-email-local:` e `aluno-email-dominio:` (da lista, no molde dos de base) e
`aluno-tg-nome:`, `aluno-tg-palavra:`, `aluno-tg-username:` e `aluno-tg-id:` (das contas, no molde dos
remetentes). Eles só entram com `prefixes` que os cite (ex.: `prefixes=STUDENT_PREFIXES`); sem `prefixes` e
com `prefixes=ALL_PREFIXES`, o resultado não tem nenhum alvo de aluno. Os alvos de aluno são disjuntos
dos de base e dos do Telegram (autoteste por detecção cruzada em `tests/unit/test_canary_helper_students.py`), e os
alvos da lista são disjuntos dos das contas (o termo promete que o nome do perfil e o `@username` não
são guardados; eles não têm exceção em lugar nenhum, nem em `identities`).

**Disparo:** dois alunos canário
próprios do disparo (`CanaryStudent`: `DISPATCH_A`, com 2 contas do Telegram, e `DISPATCH_B`, com 1), no
molde dos alunos, com contas próprias (`TG_DISPATCH_A1`, `TG_DISPATCH_A2`, `TG_DISPATCH_B`: ids de 13 dígitos na
faixa `76…`, que não contêm nem estão contidos nos ids e RAs canário anteriores nem nos RAs do disparo, e nome,
sobrenome e `@username` inconfundíveis). Os alvos (`DISPATCH_TARGETS`) têm os prefixos `disparo-*` de
`DISPATCH_PREFIXES`, fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`: `disparo-nome:`, `disparo-palavra:`,
`disparo-ra:`, `disparo-email:`, `disparo-email-local:` e `disparo-email-dominio:` (da lista) e
`disparo-tg-nome:`, `disparo-tg-palavra:`, `disparo-tg-username:` e `disparo-tg-id:` (das contas). Eles só
entram com `prefixes` que os cite (ex.: `prefixes=DISPATCH_PREFIXES`); sem `prefixes`, com `ALL_PREFIXES` ou com
`STUDENT_PREFIXES`, o resultado não tem nenhum alvo do disparo. Prefixos próprios deixam as varreduras
congeladas das outras fases iguais (o texto livre do aluno, o `chat_id` das edições e as colunas do texto digitado não
as acusam), e a fase do disparo declara as suas exceções com `DISPATCH_PREFIXES`. Os alvos do disparo são disjuntos dos
anteriores (nenhum acha o texto canário do outro).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.parse
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CanaryPerson:
    name: str
    ra: str | None
    email: str | None

    def payload(self) -> dict:
        """Corpo do cadastro (`people.register`)."""
        body: dict = {"name": self.name}
        if self.ra is not None:
            body["ra"] = self.ra
        if self.email is not None:
            body["email"] = self.email
        return body


CANARY_PEOPLE: tuple[CanaryPerson, ...] = (
    CanaryPerson("Zéfiro Canário Quintela", "9900112233", "zefiro.canario@canario.example.com"),
    CanaryPerson("Úrsula Pintassilgo Bethânia", "9900445566", None),
    CanaryPerson("Olímpio Sanhaçu Tavares", None, "olimpio.sanhacu@canario.example.com"),
)

ZEFIRO, URSULA, OLIMPIO = CANARY_PEOPLE


@dataclass(frozen=True)
class CanaryTelegramUser:
    """Remetente canário do Telegram: nome, sobrenome e `@username`
    inconfundíveis e id na faixa própria `73…` (13 dígitos), fora dos ids da Bot API falsa.

    `as_user()` e `as_private_chat()` têm a forma do `User` e do `Chat` privado da Bot API (os
    mesmos métodos do `TelegramUser` neutro de `tests/helpers/bot_api.py`), para os `push_*`.
    """

    user_id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None

    def as_user(self) -> dict[str, Any]:
        """O objeto `User` do remetente (em `from`, `reply_to_message.from`, menções…)."""
        user: dict[str, Any] = {"id": self.user_id, "is_bot": False, "first_name": self.first_name}
        if self.last_name is not None:
            user["last_name"] = self.last_name
        if self.username is not None:
            user["username"] = self.username
        user["language_code"] = "pt-br"
        return user

    def as_private_chat(self) -> dict[str, Any]:
        """O `Chat` privado com o remetente (`chat.id == from.id`, suposição do dublê)."""
        chat: dict[str, Any] = {"id": self.user_id, "type": "private", "first_name": self.first_name}
        if self.last_name is not None:
            chat["last_name"] = self.last_name
        if self.username is not None:
            chat["username"] = self.username
        return chat

    @property
    def full_name(self) -> str:
        return self.first_name if self.last_name is None else f"{self.first_name} {self.last_name}"


CANARY_TELEGRAM_USERS: tuple[CanaryTelegramUser, ...] = (
    CanaryTelegramUser(7300112233445, "Teodorico", "Maracanã Albuquerque", "teodorico_maracana_tg"),
    CanaryTelegramUser(7300556677889, "Filisbina"),
    # Mesmo nome de uma pessoa canário cadastrada (Zéfiro Canário Quintela).
    CanaryTelegramUser(7300998877665, "Zéfiro", "Canário Quintela", "zefiro_quintela_tg"),
)

TG_TEODORICO, TG_FILISBINA, TG_ZEFIRO = CANARY_TELEGRAM_USERS

# Prefixos dos rótulos dos alvos. Sem `prefixes`, `find_canary` usa exatamente os alvos de base.
BASE_PREFIXES: tuple[str, ...] = ("nome", "palavra", "ra", "email", "email-local")
TG_PREFIXES: tuple[str, ...] = ("tg-nome", "tg-palavra", "tg-username", "tg-id")
ALL_PREFIXES: tuple[str, ...] = BASE_PREFIXES + TG_PREFIXES


def _drop(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat[0] == "M" or cat == "Cf"


def fold(text: str) -> str:
    """Normalização própria dos testes: NFKD, sem marcas/Cf, casefold, espaços colapsados."""
    out: list[str] = []
    for ch in text:
        for c in unicodedata.normalize("NFKD", ch):
            if _drop(c):
                continue
            for c2 in unicodedata.normalize("NFKD", c.casefold()):
                if not _drop(c2):
                    out.append(c2)
    return re.sub(r"\s+", " ", "".join(out))


def _targets() -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for person in CANARY_PEOPLE:
        targets.append((f"nome:{person.name}", fold(person.name)))
        for word in fold(person.name).split(" "):
            if len(word) >= 5:
                targets.append((f"palavra:{word}", word))
        if person.ra is not None:
            targets.append((f"ra:{person.ra}", person.ra))
        if person.email is not None:
            targets.append((f"email:{person.email}", fold(person.email)))
            targets.append((f"email-local:{person.email}", fold(person.email.split("@")[0])))
    return targets


TARGETS = _targets()


def _tg_targets() -> list[tuple[str, str]]:
    """Alvos dos remetentes canário do Telegram (rótulos `tg-nome:`, `tg-palavra:`, `tg-username:` e
    `tg-id:`); nunca entram em `TARGETS`."""
    targets: list[tuple[str, str]] = []
    for user in CANARY_TELEGRAM_USERS:
        names = [user.full_name, user.first_name]
        if user.last_name is not None:
            names.append(user.last_name)
        for name in dict.fromkeys(names):
            targets.append((f"tg-nome:{name}", fold(name)))
        for word in fold(user.full_name).split(" "):
            if len(word) >= 5:
                targets.append((f"tg-palavra:{word}", word))
        if user.username is not None:
            # O alvo sem `@` também acha `@username` (subcadeia).
            targets.append((f"tg-username:{user.username}", fold(user.username)))
        targets.append((f"tg-id:{user.user_id}", str(user.user_id)))
    return list(dict.fromkeys(targets))


TG_TARGETS = _tg_targets()


@dataclass(frozen=True)
class CanaryStudent:
    """Aluno canário: a linha da lista da turma (nome, RA e e-mail) e as
    contas próprias do Telegram, cada uma um `CanaryTelegramUser` com nome, sobrenome e `@username`
    inconfundíveis, diferentes do nome da lista (os alvos da lista e os das contas não se cruzam)."""

    name: str
    ra: str
    email: str
    accounts: tuple[CanaryTelegramUser, ...]

    def row(self) -> dict[str, str]:
        """A linha da lista (item de `rows` do pedido de `students.import`)."""
        return {"name": self.name, "ra": self.ra, "email": self.email}


TG_STUDENT_A1 = CanaryTelegramUser(7561203948571, "Ubirajara", "Coivara Jacarandá", "ubirajara_coivara_tg")
TG_STUDENT_A2 = CanaryTelegramUser(7568837201946, "Anacleta", "Mandacaru Piracema", "anacleta_piracema_tg")
TG_STUDENT_B = CanaryTelegramUser(7574419302658, "Bartolomeu", "Guaxinim Caraúna", "bartolomeu_carauna_tg")

CANARY_STUDENTS: tuple[CanaryStudent, ...] = (
    CanaryStudent("Leocádia Jurubeba Tabatinga", "8812047356", "Leocadia.JURUBEBA@Jequitiba.Example.ORG",
                  (TG_STUDENT_A1, TG_STUDENT_A2)),
    CanaryStudent("Genésio Urutau Paraguaçu", "8826193407", "GENESIO.Urutau@Buritizal.Example.NET",
                  (TG_STUDENT_B,)),
)

STUDENT_A, STUDENT_B = CANARY_STUDENTS

# As 3 contas do Telegram dos alunos canário, na ordem dos alunos.
CANARY_STUDENT_ACCOUNTS: tuple[CanaryTelegramUser, ...] = tuple(
    account for student in CANARY_STUDENTS for account in student.accounts)

# Prefixos dos alvos dos alunos canário: fora de `ALL_PREFIXES`, que não os inclui.
STUDENT_PREFIXES: tuple[str, ...] = (
    "aluno-nome", "aluno-palavra", "aluno-ra", "aluno-email", "aluno-email-local", "aluno-email-dominio",
    "aluno-tg-nome", "aluno-tg-palavra", "aluno-tg-username", "aluno-tg-id",
)


def _student_targets() -> list[tuple[str, str]]:
    """Alvos dos alunos canário (rótulos com os prefixos de `STUDENT_PREFIXES`); nunca entram em
    `TARGETS` nem em `TG_TARGETS`.

    Da lista, no molde dos alvos de base: nome completo, cada palavra do nome com 5 letras ou mais, RA, e-mail,
    parte local e domínio do e-mail. De cada conta, no molde dos remetentes do Telegram: nome, sobrenome e os dois juntos,
    cada palavra de 5 letras ou mais, `username` (acha também o `@username`) e id.
    """
    targets: list[tuple[str, str]] = []
    for student in CANARY_STUDENTS:
        targets.append((f"aluno-nome:{student.name}", fold(student.name)))
        for word in fold(student.name).split(" "):
            if len(word) >= 5:
                targets.append((f"aluno-palavra:{word}", word))
        targets.append((f"aluno-ra:{student.ra}", student.ra))
        local, domain = student.email.split("@")
        targets.append((f"aluno-email:{student.email}", fold(student.email)))
        targets.append((f"aluno-email-local:{student.email}", fold(local)))
        targets.append((f"aluno-email-dominio:{student.email}", fold(domain)))
        for account in student.accounts:
            names = [account.full_name, account.first_name]
            if account.last_name is not None:
                names.append(account.last_name)
            for name in dict.fromkeys(names):
                targets.append((f"aluno-tg-nome:{name}", fold(name)))
            for word in fold(account.full_name).split(" "):
                if len(word) >= 5:
                    targets.append((f"aluno-tg-palavra:{word}", word))
            if account.username is not None:
                # O alvo sem `@` também acha `@username` (subcadeia).
                targets.append((f"aluno-tg-username:{account.username}", fold(account.username)))
            targets.append((f"aluno-tg-id:{account.user_id}", str(account.user_id)))
    return list(dict.fromkeys(targets))


STUDENT_TARGETS = _student_targets()


# --- alunos canário do disparo ----------------------------------

TG_DISPATCH_A1 = CanaryTelegramUser(7612049385716, "Epaminondas", "Sucupira Tamboril", "epaminondas_sucupira_tg")
TG_DISPATCH_A2 = CanaryTelegramUser(7629318405672, "Hermengarda", "Juazeiro Capivari", "hermengarda_capivari_tg")
TG_DISPATCH_B = CanaryTelegramUser(7637520864193, "Petronilha", "Caatinga Urucuri", "petronilha_urucuri_tg")

CANARY_DISPATCH_STUDENTS: tuple[CanaryStudent, ...] = (
    CanaryStudent("Cremilda Quixabeira Itaúna", "6631058274", "Cremilda.QUIXABEIRA@Carnaubal.Example.ORG",
                  (TG_DISPATCH_A1, TG_DISPATCH_A2)),
    CanaryStudent("Deusdédit Jenipapo Taquaruçu", "6648920317", "DEUSDEDIT.Jenipapo@Umbuzeiro.Example.NET",
                  (TG_DISPATCH_B,)),
)

DISPATCH_A, DISPATCH_B = CANARY_DISPATCH_STUDENTS

# As 3 contas do Telegram dos alunos canário do disparo, na ordem dos alunos.
CANARY_DISPATCH_ACCOUNTS: tuple[CanaryTelegramUser, ...] = tuple(
    account for student in CANARY_DISPATCH_STUDENTS for account in student.accounts)

# Prefixos dos alvos dos alunos canário do disparo: fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`, que não os
# incluem.
DISPATCH_PREFIXES: tuple[str, ...] = (
    "disparo-nome", "disparo-palavra", "disparo-ra", "disparo-email", "disparo-email-local",
    "disparo-email-dominio", "disparo-tg-nome", "disparo-tg-palavra", "disparo-tg-username", "disparo-tg-id",
)


def _dispatch_targets() -> list[tuple[str, str]]:
    """Alvos dos alunos canário do disparo (rótulos com os prefixos de `DISPATCH_PREFIXES`); nunca entram em
    `TARGETS`, `TG_TARGETS` nem `STUDENT_TARGETS`.

    O mesmo molde de `_student_targets`: da lista, nome completo, cada palavra do nome com 5 letras ou
    mais, RA, e-mail, parte local e domínio do e-mail; de cada conta, nome, sobrenome e os dois juntos,
    cada palavra de 5 letras ou mais, `username` (acha também o `@username`) e id.
    """
    targets: list[tuple[str, str]] = []
    for student in CANARY_DISPATCH_STUDENTS:
        targets.append((f"disparo-nome:{student.name}", fold(student.name)))
        for word in fold(student.name).split(" "):
            if len(word) >= 5:
                targets.append((f"disparo-palavra:{word}", word))
        targets.append((f"disparo-ra:{student.ra}", student.ra))
        local, domain = student.email.split("@")
        targets.append((f"disparo-email:{student.email}", fold(student.email)))
        targets.append((f"disparo-email-local:{student.email}", fold(local)))
        targets.append((f"disparo-email-dominio:{student.email}", fold(domain)))
        for account in student.accounts:
            names = [account.full_name, account.first_name]
            if account.last_name is not None:
                names.append(account.last_name)
            for name in dict.fromkeys(names):
                targets.append((f"disparo-tg-nome:{name}", fold(name)))
            for word in fold(account.full_name).split(" "):
                if len(word) >= 5:
                    targets.append((f"disparo-tg-palavra:{word}", word))
            if account.username is not None:
                # O alvo sem `@` também acha `@username` (subcadeia).
                targets.append((f"disparo-tg-username:{account.username}", fold(account.username)))
            targets.append((f"disparo-tg-id:{account.user_id}", str(account.user_id)))
    return list(dict.fromkeys(targets))


DISPATCH_TARGETS = _dispatch_targets()


# --- pessoas canário do expurgo ----------------------
#
# A semente do expurgo (`tests/helpers/purge.py`, `seed_purge_scenario`) roda também na fase do expurgo do canário,
# no mesmo banco das fases anteriores: as pessoas dela precisam ser **novas** (o expurgo apaga a
# identidade das que só estão no oferecimento expurgado, e as das fases anteriores seguem em uso nas fases seguintes) e
# com alvos próprios, para que as varreduras e as exceções declaradas das fases anteriores não mudem. No molde do disparo:
# quatro pessoas (`CanaryStudent`, a linha da lista e as contas próprias do Telegram, ids de 13 dígitos na faixa
# `77…`, que não contêm nem estão contidos nos ids e RAs canário anteriores) e os dois e-mails de contato do
# professor (o antigo e o vigente). Os alvos (`PURGE_TARGETS`) têm os prefixos `expurgo-*` de `PURGE_PREFIXES`,
# fora de `ALL_PREFIXES`, `STUDENT_PREFIXES` e `DISPATCH_PREFIXES`: sem `prefixes`, ou com qualquer coleção de prefixos
# anterior, `find_canary` não devolve nenhum alvo do expurgo.

TG_PURGE_A1 = CanaryTelegramUser(7712046385931, "Juvenal", "Mutum Aroeira", "juvenal_mutum_tg")
TG_PURGE_A2 = CanaryTelegramUser(7718350279146, "Rosmunda", "Taboca Guariroba", "rosmunda_taboca_tg")
TG_PURGE_B = CanaryTelegramUser(7725839104627, "Gumercindo", "Jatobá Ingazeira", "gumercindo_jatoba_tg")
TG_PURGE_C = CanaryTelegramUser(7738201956473, "Firmina", "Sabiá Coqueiral", "firmina_sabia_tg")
TG_PURGE_D = CanaryTelegramUser(7749302817465, "Isidoro", "Pacova Juruti", "isidoro_pacova_tg")

CANARY_PURGE_PEOPLE: tuple[CanaryStudent, ...] = (
    # apagada: aluna só do oferecimento expurgado, com duas contas (um endereço antigo e um vigente)
    CanaryStudent("Rosalvo Cambucá Itapemirim", "5512093847", "Rosalvo.CAMBUCA@Ipeuna.Example.ORG",
                  (TG_PURGE_A1, TG_PURGE_A2)),
    # mantida: aluna também de outro oferecimento ativo
    CanaryStudent("Benedita Siriema Quixeramobim", "5527481930", "BENEDITA.Siriema@Ibirama.Example.NET",
                  (TG_PURGE_B,)),
    # equipe: membro da equipe só do oferecimento expurgado
    CanaryStudent("Clodomiro Tucunaré Anhumas", "5536910284", "Clodomiro.TUCUNARE@Pirapora.Example.ORG",
                  (TG_PURGE_C,)),
    # citada: cadastrada depois das mensagens livres que a citam (o limite do texto livre gravado antes do cadastro da pessoa), só do expurgado
    CanaryStudent("Doralice Jaçanã Mucugê", "5548302917", "DORALICE.Jacana@Itaberaba.Example.NET",
                  (TG_PURGE_D,)),
)

PURGE_A, PURGE_B, PURGE_C, PURGE_D = CANARY_PURGE_PEOPLE

# As 5 contas do Telegram das pessoas canário do expurgo, na ordem das pessoas.
CANARY_PURGE_ACCOUNTS: tuple[CanaryTelegramUser, ...] = tuple(
    account for person in CANARY_PURGE_PEOPLE for account in person.accounts)

# Os e-mails de contato do professor (nunca identidade de pessoa cadastrada; a redação não os altera, então
# `offerings.update_settings` os aceita): o antigo, trocado antes do arquivamento, e o vigente.
PURGE_CONTACT_OLD = "Orientacao.Velha@Gameleira-Docente.Example.ORG"
PURGE_CONTACT_CURRENT = "Atendimento.Novo@Barauna-Docente.Example.NET"
PURGE_CONTACTS: tuple[str, ...] = (PURGE_CONTACT_OLD, PURGE_CONTACT_CURRENT)

# Prefixos dos alvos do expurgo: fora de `ALL_PREFIXES`, `STUDENT_PREFIXES` e `DISPATCH_PREFIXES`, que não os incluem.
PURGE_PREFIXES: tuple[str, ...] = (
    "expurgo-nome", "expurgo-palavra", "expurgo-ra", "expurgo-email", "expurgo-email-local",
    "expurgo-email-dominio", "expurgo-tg-nome", "expurgo-tg-palavra", "expurgo-tg-username", "expurgo-tg-id",
    "expurgo-contato", "expurgo-contato-local", "expurgo-contato-dominio",
)


def _purge_targets() -> list[tuple[str, str]]:
    """Alvos das pessoas canário do expurgo (rótulos com os prefixos de `PURGE_PREFIXES`); nunca entram em `TARGETS`,
    `TG_TARGETS`, `STUDENT_TARGETS` nem `DISPATCH_TARGETS`. O mesmo molde de `_dispatch_targets`, mais os e-mails de
    contato (`expurgo-contato:`, `expurgo-contato-local:` e `expurgo-contato-dominio:`)."""
    targets: list[tuple[str, str]] = []
    for person in CANARY_PURGE_PEOPLE:
        targets.append((f"expurgo-nome:{person.name}", fold(person.name)))
        for word in fold(person.name).split(" "):
            if len(word) >= 5:
                targets.append((f"expurgo-palavra:{word}", word))
        targets.append((f"expurgo-ra:{person.ra}", person.ra))
        local, domain = person.email.split("@")
        targets.append((f"expurgo-email:{person.email}", fold(person.email)))
        targets.append((f"expurgo-email-local:{person.email}", fold(local)))
        targets.append((f"expurgo-email-dominio:{person.email}", fold(domain)))
        for account in person.accounts:
            names = [account.full_name, account.first_name]
            if account.last_name is not None:
                names.append(account.last_name)
            for name in dict.fromkeys(names):
                targets.append((f"expurgo-tg-nome:{name}", fold(name)))
            for word in fold(account.full_name).split(" "):
                if len(word) >= 5:
                    targets.append((f"expurgo-tg-palavra:{word}", word))
            if account.username is not None:
                # O alvo sem `@` também acha `@username` (subcadeia).
                targets.append((f"expurgo-tg-username:{account.username}", fold(account.username)))
            targets.append((f"expurgo-tg-id:{account.user_id}", str(account.user_id)))
    for email in PURGE_CONTACTS:
        local, domain = email.split("@")
        targets.append((f"expurgo-contato:{email}", fold(email)))
        targets.append((f"expurgo-contato-local:{email}", fold(local)))
        targets.append((f"expurgo-contato-dominio:{email}", fold(domain)))
    return list(dict.fromkeys(targets))


PURGE_TARGETS = _purge_targets()

_KNOWN_PREFIXES = frozenset(ALL_PREFIXES + STUDENT_PREFIXES + DISPATCH_PREFIXES + PURGE_PREFIXES)


def _select_targets(prefixes: Collection[str] | None) -> list[tuple[str, str]]:
    if prefixes is None:
        return TARGETS
    if isinstance(prefixes, str) or not prefixes:
        raise ValueError("prefixes deve ser uma coleção não vazia de prefixos (ex.: TG_PREFIXES)")
    unknown = set(prefixes) - _KNOWN_PREFIXES
    if unknown:
        raise ValueError(f"prefixos desconhecidos: {sorted(unknown)}")
    wanted = set(prefixes)
    # os alvos do expurgo (`PURGE_TARGETS`) só entram com prefixos `expurgo-*`.
    return [(label, target) for label, target in TARGETS + TG_TARGETS + STUDENT_TARGETS + DISPATCH_TARGETS + PURGE_TARGETS
            if label.split(":", 1)[0] in wanted]


def _json_strings(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            found.append(str(k))
            found.extend(_json_strings(v))
    elif isinstance(value, list):
        for item in value:
            found.extend(_json_strings(item))
    elif value is not None and not isinstance(value, bool):
        found.append(str(value))
    return found


def _forms(text: str) -> list[str]:
    forms = [text, urllib.parse.unquote(text), urllib.parse.unquote_plus(text)]
    candidates = [text] + text.splitlines()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate[0] not in "[{\"":
            continue
        try:
            decoded = json.loads(candidate)
        except ValueError:
            continue
        forms.extend(_json_strings(decoded))
    return forms


def find_canary(text: str, *, prefixes: Collection[str] | None = None) -> list[str]:
    """Devolve os rótulos dos alvos canário encontrados em `text` (lista vazia se nenhum)."""
    # Sem `prefixes`, procura exatamente os alvos de base (`TARGETS`). Com `prefixes` (ex.:
    # `TG_PREFIXES`, `ALL_PREFIXES` ou `("tg-id",)`), procura os alvos de base e os dos remetentes do
    # Telegram cujo rótulo tem um desses prefixos; prefixo desconhecido é erro do teste.
    # os alvos dos alunos canário só entram com prefixos `aluno-*` (ex.: `STUDENT_PREFIXES`);
    # sem `prefixes` e com `ALL_PREFIXES`, o resultado não tem alvo de aluno.
    # os alvos dos alunos canário do disparo só entram com prefixos `disparo-*` (ex.:
    # `DISPATCH_PREFIXES`); sem `prefixes`, com `ALL_PREFIXES` ou com `STUDENT_PREFIXES`, o resultado não
    # tem alvo do disparo.
    # os alvos das pessoas canário do expurgo só entram com prefixos `expurgo-*` (ex.: `PURGE_PREFIXES`); com
    # qualquer coleção de prefixos anterior, o resultado não tem alvo do expurgo.
    targets = _select_targets(prefixes)
    if not text:
        return []
    hits: set[str] = set()
    for form in _forms(text):
        folded = fold(form)
        for label, target in targets:
            if target and target in folded:
                hits.add(label)
    return sorted(hits)


def find_secrets(text: str, secrets: list[str]) -> list[str]:
    """Procura segredos de token (e o SHA-256 hex de cada um) em `text`, também em JSON escapado."""
    if not text:
        return []
    hits: set[str] = set()
    forms = _forms(text)
    for secret in secrets:
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        for form in forms:
            if secret in form:
                hits.add(f"segredo:{secret[:6]}…")
            if digest in form.lower():
                hits.add(f"resumo:{digest[:8]}…")
    return sorted(hits)
