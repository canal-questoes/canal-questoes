"""Testes das unidades do systemd do backup.


As unidades são lidas por um leitor próprio, escrito aqui: seções `[X]`, linhas `chave=valor`, comentários
`#`/`;`, continuação com `\\` no fim da linha, e **chaves repetidas guardadas em lista** (as `Environment=`).
**[F]** O `configparser` não serve: o estrito levanta `DuplicateOptionError` e o não estrito guarda só a
última (Python 3.12.3). O valor de cada `Environment=` é dividido como o systemd faz com as
atribuições entre aspas (aqui por `shlex`, POSIX).

O `ExecStart` do serviço, com cada `${VAR}` trocada pelo valor da `Environment=`, tem de ser aceito pelo
analisador de argumentos do `cq_backup.py` (carregado por `load_ops`): o teste troca o `run` do módulo por um
registrador e confere o `BackupConfig` montado. **[I]** Os nomes das variáveis (fora o `CQ_BACKUP_PROJECT`, que
o teste fixa) são livres; o teste exige só que cada um dos cinco parâmetros venha de uma `${…}` definida
numa `Environment=`, com o valor esperado.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from tests.helpers.ops import REPO, clean_env, load_ops, run_main

SERVICE = REPO / "ops" / "backup" / "cq-backup.service"
TIMER = REPO / "ops" / "backup" / "cq-backup.timer"

PYTHON = "/usr/bin/python3"
SCRIPT = "/srv/cq/app/ops/backup/cq_backup.py"
# cada parâmetro do `ExecStart` e o valor da `Environment=` que o alimenta.
EXPECTED_PARAMETERS = {
    "--project": "cq",
    "--recipients": "/etc/cq-backup/recipients.txt",
    "--dumps-dir": "/var/backups/cq/dumps",
    "--state-dir": "/var/backups/cq/state",
    "--keep-days": "7",
}
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

Unit = dict[str, dict[str, list[str]]]


def read_unit(path: Path) -> Unit:
    """Lê uma unidade do systemd: `{seção: {chave: [valores na ordem]}}` (ver o docstring do módulo)."""
    assert path.is_file(), f"unidade ausente: {path.relative_to(REPO).as_posix()}"
    sections: Unit = {}
    current: str | None = None
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = pending + raw.strip() if pending else raw.strip()
        pending = ""
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        assert current is not None, "linha fora de seção"
        key, sep, value = line.partition("=")
        assert sep, f"linha sem '=' na seção [{current}]"
        sections[current].setdefault(key.strip(), []).append(value.strip())
    assert not pending, "continuação sem linha seguinte"
    return sections


def _one(unit: Unit, section: str, key: str) -> str:
    values = unit.get(section, {}).get(key, [])
    assert len(values) == 1, f"[{section}] {key}= deve aparecer uma vez (achei {len(values)})"
    return values[0]


def _environment(unit: Unit) -> dict[str, str]:
    """As atribuições de todas as `Environment=` do `[Service]` (nome → valor), sem repetição."""
    assignments: dict[str, str] = {}
    for value in unit.get("Service", {}).get("Environment", []):
        for item in shlex.split(value, posix=True):
            name, sep, assigned = item.partition("=")
            assert sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name), "Environment= malformada"
            assert name not in assignments, f"variável repetida: {name}"
            assignments[name] = assigned
    return assignments


def _exec_start(unit: Unit) -> list[str]:
    return shlex.split(_one(unit, "Service", "ExecStart"), posix=True)


def test_timer_keys() -> None:
    """`OnCalendar=*-*-* 03:30:00 UTC`, `Persistent=true`, `RandomizedDelaySec=10min` e
    `[Install] WantedBy=timers.target`."""
    timer = read_unit(TIMER)

    assert _one(timer, "Timer", "OnCalendar") == "*-*-* 03:30:00 UTC"
    assert _one(timer, "Timer", "Persistent") == "true"
    assert _one(timer, "Timer", "RandomizedDelaySec") == "10min"
    assert _one(timer, "Install", "WantedBy") == "timers.target"


def test_service_keys_and_environment() -> None:
    """`Type=oneshot`, `User=__CQ_OPERATOR__` (o guia troca o marcador), o `ExecStart` com o
    `/usr/bin/python3` direto e o script em `/srv/cq/app`, os cinco parâmetros da rotina por `${…}` de
    `Environment=` (o projeto por `${CQ_BACKUP_PROJECT}`), cada `Environment=` conferida, nenhum `--age-bin` nem
    `--docker-bin`."""
    service = read_unit(SERVICE)

    assert _one(service, "Service", "Type") == "oneshot"
    assert _one(service, "Service", "User") == "__CQ_OPERATOR__"
    argv = _exec_start(service)
    assert argv[:3] == [PYTHON, SCRIPT, "run"]
    options = argv[3:]
    assert len(options) == 2 * len(EXPECTED_PARAMETERS), "ExecStart com argumentos a mais ou a menos"
    pairs = dict(zip(options[0::2], options[1::2]))
    assert set(pairs) == set(EXPECTED_PARAMETERS)
    assert pairs["--project"] == "${CQ_BACKUP_PROJECT}"
    environment = _environment(service)
    used: dict[str, str] = {}
    for option, token in pairs.items():
        match = VARIABLE.fullmatch(token)
        assert match is not None, f"{option} deve vir de uma ${{…}} de Environment="
        name = match.group(1)
        assert name in environment, f"{option}: variável sem Environment="
        assert environment[name] == EXPECTED_PARAMETERS[option], f"{option}: valor da Environment= errado"
        used[name] = environment[name]
    assert environment == used, "Environment= que o ExecStart não usa"


def test_service_has_no_environment_file_nor_env_mention() -> None:
    """sem `EnvironmentFile=` em nenhuma seção e sem nenhuma menção a `.env` no arquivo."""
    service = read_unit(SERVICE)

    for section, keys in service.items():
        assert "EnvironmentFile" not in keys, f"EnvironmentFile= em [{section}]"
    text = SERVICE.read_text(encoding="utf-8")
    assert re.search(r"\.env\b", text) is None
    assert "EnvironmentFile" not in text


def test_exec_start_is_accepted_by_the_routine_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """o `ExecStart`, com as `${…}` trocadas pelos valores das `Environment=`, é aceito pelo
    analisador do `cq_backup.py` e monta o `BackupConfig` esperado (padrões `age` e `docker`). Falha
    no esqueleto, onde o `main` lança `NotImplementedError`."""
    service = read_unit(SERVICE)
    environment = _environment(service)
    argv = [VARIABLE.sub(lambda match: environment[match.group(1)], token) for token in _exec_start(service)]
    assert argv[:2] == [PYTHON, SCRIPT]
    cq_backup = load_ops("ops/backup/cq_backup.py")
    seen: list[object] = []

    def fake_run(*args: object, **kwargs: object) -> int:
        seen.append(args[0] if args else kwargs["config"])
        return 0

    monkeypatch.setattr(cq_backup, "run", fake_run)

    result = run_main(cq_backup, argv[2:], env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (0, "", "")
    assert seen == [cq_backup.BackupConfig(
        project="cq", recipients=Path("/etc/cq-backup/recipients.txt"), dumps_dir=Path("/var/backups/cq/dumps"),
        state_dir=Path("/var/backups/cq/state"), keep_days=7, age_bin="age", docker_bin="docker")]
