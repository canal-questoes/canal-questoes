"""A rotina de backup com o **age real**.


O `age` é o v1.3.2 oficial do cache (`tests/helpers/age_bin.py`, `CQ_TEST_AGE_DIR`); o `pg_dump` é falso, mas
roda num processo real (o modo misto do `FakeSpawn`: o `docker ps` falso, o `docker exec … pg_dump` num
`sys.executable -c` que escreve os bytes fixos e o argv do `age` entregue ao `subprocess.Popen` real com o
stdout desse processo como stdin). Todo argv é conferido com `==`. Sem o age no cache, o teste falha com a
mensagem de ambiente (nunca `skip`).

Os detectores leem a identidade do `tmp_path` só para procurá-la nas saídas (a regra do `age_bin.py`).
"""

from __future__ import annotations

import io
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.helpers.age_bin import age_paths, keypair
from tests.helpers.canary import find_secrets
from tests.helpers.ops import clean_env, load_ops
from tests.helpers.process_fake import AGE_HEADER, FakeSpawn, docker_ps_argv, pg_dump_argv, sha256_hex
from tests.helpers.secrets import MASTER_KEY

cq_backup = load_ops("ops/backup/cq_backup.py")

PROJECT = "cq-teste-age-real"
CONTAINER = "5b4a3c2d1e0f"
NOW = datetime(2026, 10, 29, 3, 30, 5, tzinfo=UTC)
NOW_NAME = "cq-20261029T033005Z.dump.age"
# Bytes de um dump falso do `pg_custom` (começam com `PGDMP`), com um trecho binário e um texto marcador.
DUMP = b"PGDMP\x01\x0e\x00\x04\x08\x01\x01" + bytes(range(256)) + b"cq-dump-real-age;" * 64
DECRYPT_TIMEOUT_SECONDS = 60


def _secret_key(identity: Path) -> str:
    """A linha `AGE-SECRET-KEY-1…` da identidade (lida só para os detectores)."""
    for line in identity.read_text(encoding="ascii").splitlines():
        if line.startswith("AGE-SECRET-KEY-1"):
            return line.strip()
    raise AssertionError("a identidade gerada não tem a linha AGE-SECRET-KEY-1")


def _all_bytes(*dirs: Path) -> bytes:
    data = b""
    for directory in dirs:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                data += path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0"
    return data


def test_backup_with_real_age_decrypts_to_the_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """o `run` com o age real e um `pg_dump` falso grava `cq-<carimbo>.dump.age`, que
    começa com `age-encryption.org/v1` e decifra com `age -d -i <identidade>` exatamente nos bytes do `pg_dump`;
    o `SHA256SUMS` e o `last-backup.json` conferem com o arquivo; nenhum arquivo com `PGDMP` no `tmp_path`; a
    identidade e a chave-mestra do teste não aparecem em nada que a rotina escreve (dumps, estado, stdout,
    stderr)."""
    age, _ = age_paths()
    (tmp_path / "chave").mkdir()
    identity, recipient = keypair(tmp_path / "chave")
    secret_key = _secret_key(identity)
    monkeypatch.setenv("CQ_MASTER_KEY", MASTER_KEY)

    vm = tmp_path / "vm"
    dumps, state = vm / "dumps", vm / "state"
    dumps.mkdir(parents=True)
    state.mkdir(parents=True)
    recipients = vm / "recipients.txt"
    recipients.write_text(f"# destinatario do teste\n\n{recipient}\n", encoding="utf-8")

    spawn = FakeSpawn(passthrough=str(age))
    spawn.on_docker_ps([CONTAINER], project=PROJECT)
    spawn.on_pg_dump(DUMP)
    out, err = io.StringIO(), io.StringIO()
    config = cq_backup.BackupConfig(project=PROJECT, recipients=recipients, dumps_dir=dumps, state_dir=state,
                                    age_bin=str(age))

    code = cq_backup.run(config, spawn=spawn, now=lambda: NOW, stdout=out, stderr=err)

    assert (code, out.getvalue(), err.getvalue()) == (0, "Backup concluído.\n", "")
    argvs = spawn.argvs
    assert len(argvs) == 3
    assert argvs[0] == docker_ps_argv(PROJECT)
    assert argvs[1] == pg_dump_argv(CONTAINER)
    assert argvs[2][:4] == [str(age), "-R", str(recipients), "-o"] and len(argvs[2]) == 5
    temporary = Path(argvs[2][4])
    assert temporary.parent == dumps and re.fullmatch(r"\.tmp-.+-\d+\.age", temporary.name)
    assert [call.real for call in spawn.calls] == [False, True, True]

    assert sorted(path.name for path in dumps.iterdir()) == sorted([NOW_NAME, "SHA256SUMS"])
    final = (dumps / NOW_NAME).read_bytes()
    assert final.startswith(AGE_HEADER)
    assert b"PGDMP" not in final
    decrypted = subprocess.run([str(age), "-d", "-i", str(identity), str(dumps / NOW_NAME)], env=clean_env(),
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=DECRYPT_TIMEOUT_SECONDS,
                               check=False)
    assert decrypted.returncode == 0
    assert decrypted.stdout == DUMP

    assert (dumps / "SHA256SUMS").read_text(encoding="utf-8") == f"{sha256_hex(final)}  {NOW_NAME}\n"
    record = json.loads((state / "last-backup.json").read_text(encoding="utf-8"))
    assert record == {"version": 1, "finished_at": "2026-10-29T03:30:05Z", "file": NOW_NAME,
                      "size_bytes": len(final), "sha256": sha256_hex(final)}

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"PGDMP" not in path.read_bytes(), f"dump em claro em disco: {path.name}"
    written = _all_bytes(dumps, state)
    assert secret_key.encode("ascii") not in written
    assert MASTER_KEY.encode("ascii") not in written
    assert find_secrets(out.getvalue() + err.getvalue() + written.decode("latin-1"), [secret_key, MASTER_KEY]) == []
    for call in spawn.calls:
        assert call.env is not None and "CQ_MASTER_KEY" not in {name.upper() for name in call.env}
