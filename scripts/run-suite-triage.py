"""Suíte inteira com triagem automática das falhas.

Roda `scripts/run-suite.sh` (ou lê uma saída já gravada), reexecuta isoladamente cada falha e classifica:
- `falha_real`: falhou em alguma reexecução isolada;
- `instavel_conhecido`: passou em todas as reexecuções e está na lista de instáveis conhecidos;
- `instavel_novo`: passou em todas as reexecuções, mas não está na lista;
- `ambiente`: o pytest não chegou a rodar o teste isolado (por exemplo, o Postgres de teste ou o Docker fora do
  ar); é preciso consertar o ambiente e rodar de novo;
- `nao_reexecutado`: acima do limite de reexecuções (`--max-reruns`); o limite é sempre informado.
Grava um JSON com as contagens por diretório, as falhas e o congelamento. Sai com 1 se houver falha real, falha não
reexecutada ou congelamento divergente.

Uso:
  python scripts/run-suite-triage.py --label <rótulo> --frozen <dir com testes.sha256>
  python scripts/run-suite-triage.py --from <dir de uma suíte já rodada> --frozen <dir com testes.sha256>
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import defusedxml.ElementTree as ET
except ImportError:  # a entrada é o JUnit que o próprio pytest grava localmente (confiável)
    import xml.etree.ElementTree as ET  # noqa: S405

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           check=True).stdout.strip())
KNOWN = ROOT / "docs" / "execucao" / "instaveis-conhecidos.txt"


def load_known() -> tuple[list[tuple[str, str]], list[tuple[re.Pattern[str], str]]]:
    nodes, patterns = [], []
    if not KNOWN.exists():
        return nodes, patterns
    for line in KNOWN.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, reason = line.partition("\t")
        if key.startswith("re:"):
            patterns.append((re.compile(key[3:]), reason))
        else:
            nodes.append((key.strip(), reason))
    return nodes, patterns


def known_reason(nodeid: str, message: str, nodes, patterns) -> str | None:
    base = nodeid.split("[", 1)[0]
    for key, reason in nodes:
        if nodeid == key or base == key or nodeid.startswith(key + "::") or (key.endswith(".py") and
                                                                            nodeid.startswith(key)):
            return reason
    for pattern, reason in patterns:
        if pattern.search(message):
            return reason
    return None


def nodeid_of(classname: str, name: str) -> str:
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        path = Path(*parts[:i]).with_suffix(".py")
        if (ROOT / path).exists():
            return "::".join([path.as_posix(), *parts[i:], name])
    return f"{classname.replace('.', '/')}.py::{name}"


def parse(out_dir: Path) -> tuple[dict, list[dict]]:
    counts, failures = {}, []
    for xml in sorted(out_dir.glob("*.xml")):
        root = ET.parse(xml).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        c = {"testes": 0, "falhas": 0, "erros": 0, "pulados": 0}
        for s in suites:
            c["testes"] += int(s.get("tests", 0))
            c["falhas"] += int(s.get("failures", 0))
            c["erros"] += int(s.get("errors", 0))
            c["pulados"] += int(s.get("skipped", 0))
            for case in s.iter("testcase"):
                bad = case.find("failure")
                if bad is None:
                    bad = case.find("error")
                if bad is None:
                    continue
                message = (bad.get("message") or "") + "\n" + (bad.text or "")
                failures.append({"no": nodeid_of(case.get("classname", ""), case.get("name", "")),
                                 "parte": xml.stem, "tipo": bad.tag, "mensagem": message.strip()[:600]})
        counts[xml.stem] = c
    # O JUnit às vezes não registra a falha que o resumo do pytest mostra (visto nos testes que criam
    # subprocessos): as linhas FAILED/ERROR do texto completam a lista. Sem mensagem na linha, vale a das linhas
    # `E` da seção do teste (`___ nome ___`), para que os padrões dos instáveis conhecidos a reconheçam.
    seen = {f["no"] for f in failures}
    for txt in sorted(out_dir.glob("*.txt")):
        lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()
        sections, current = {}, None
        for line in lines:
            header = re.match(r"^_+ (\S+) _+$", line)
            if header:
                current = sections.setdefault(header.group(1), [])
            elif line.startswith("====="):
                current = None
            elif current is not None and line.startswith("E "):
                current.append(line[1:].strip())
        for line in lines:
            m = re.match(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", line)
            if m and "::" in m.group(2) and m.group(2) not in seen:
                seen.add(m.group(2))
                name = m.group(2).split("::", 1)[1].replace("::", ".")
                message = (m.group(3) or "").strip() or "\n".join(sections.get(name, []))
                failures.append({"no": m.group(2), "parte": txt.stem, "tipo": m.group(1).lower(),
                                 "mensagem": message[:600]})
    return counts, failures


def docker_running() -> bool:
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True,
                              text=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def rerun(nodeid: str, times: int) -> tuple[list[str], str]:
    """Cada reexecução dá `ok`, `falha` (o pytest rodou o teste e ele falhou) ou `nao_rodou` (o pytest não chegou
    a rodá-lo: ambiente indisponível, erro de coleta ou de uso; códigos de saída 2 a 5)."""
    cmd = ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", nodeid]
    if nodeid.startswith("tests/system/"):
        cmd += ["-m", "system"]
    env = {**os.environ, "PYTHONUTF8": "1"}
    results, last = [], ""
    for _ in range(times):
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
        results.append({0: "ok", 1: "falha"}.get(proc.returncode, "nao_rodou"))
        tail = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
        last = tail[-1][:300] if tail else ""
    return results, last


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", default="triage", help="rótulo da execução (subdiretório da saída)")
    ap.add_argument("--from", dest="from_dir", type=Path, help="usar a saída de uma suíte já rodada")
    ap.add_argument("--frozen", help="diretório da spec com testes.sha256 (confere o congelamento)")
    ap.add_argument("--no-system", action="store_true", help="pular o teste de sistema (Compose)")
    ap.add_argument("--reruns", type=int, default=2, help="reexecuções isoladas por falha (padrão 2)")
    ap.add_argument("--max-reruns", type=int, default=40, help="máximo de falhas reexecutadas (padrão 40)")
    ap.add_argument("--out", type=Path, help="arquivo JSON (padrão: <saída>/triagem.json)")
    args = ap.parse_args()

    if args.from_dir:
        out_dir = args.from_dir
    else:
        base = Path(os.environ.get("CQ_SUITE_OUT") or Path(tempfile.gettempdir()) / "cq-suite")
        env = {**os.environ, "PYTHONUTF8": "1", "CQ_SUITE_OUT": str(base), "SYSTEM": "0" if args.no_system else "1"}
        env.pop("FROZEN", None)
        subprocess.run(["bash", "scripts/run-suite.sh", args.label], cwd=ROOT, env=env)
        out_dir = base / args.label

    counts, failures = parse(out_dir)
    nodes, patterns = load_known()
    skipped = max(0, len(failures) - args.max_reruns)
    for i, f in enumerate(failures):
        f["conhecido"] = known_reason(f["no"], f["mensagem"], nodes, patterns)
        if i >= args.max_reruns:
            f["classe"] = "nao_reexecutado"
            continue
        f["isolado"], f["ultima_linha"] = rerun(f["no"], args.reruns)
        if all(r == "ok" for r in f["isolado"]):
            f["classe"] = "instavel_conhecido" if f["conhecido"] else "instavel_novo"
        elif "falha" in f["isolado"]:
            f["classe"] = "falha_real"
        else:
            f["classe"] = "ambiente"

    frozen = None
    if args.frozen:
        proc = subprocess.run(["bash", "scripts/check-frozen-tests.sh", args.frozen], cwd=ROOT,
                              capture_output=True, text=True)
        frozen = {"ok": proc.returncode == 0, "saida": (proc.stdout + proc.stderr).strip()[-800:]}

    total = {k: sum(c[k] for c in counts.values()) for k in ("testes", "falhas", "erros", "pulados")}
    total["falhas"] = max(total["falhas"] + total["erros"], len(failures)) - total["erros"]
    docker = docker_running() if any(f["no"].startswith("tests/system/") for f in failures) else None
    by_class: dict[str, int] = {}
    for f in failures:
        by_class[f["classe"]] = by_class.get(f["classe"], 0) + 1
    report = {"saida": str(out_dir), "partes": counts, "total": total, "por_classe": by_class,
              "nao_reexecutadas": skipped, "congelamento": frozen, "docker_ativo": docker, "falhas": failures}
    out = args.out or out_dir / "triagem.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = total["testes"] - total["falhas"] - total["erros"] - total["pulados"]
    print(f"{passed}/{total['testes']} passaram; falhas por classe: {by_class or 'nenhuma'}")
    if skipped:
        print(f"ATENÇÃO: {skipped} falhas não foram reexecutadas (limite --max-reruns {args.max_reruns})")
    for f in failures:
        if f["classe"] in ("falha_real", "instavel_novo", "nao_reexecutado", "ambiente"):
            detail = f.get("ultima_linha") if f["classe"] == "ambiente" else (f["mensagem"].splitlines() or [""])[0]
            print(f"- [{f['classe']}] {f['no']}: {(detail or '')[:160]}")
    if docker is False:
        print("ATENÇÃO: o Docker não está rodando; as falhas de tests/system podem ser de ambiente")
    if frozen is not None:
        print(f"congelamento: {'ok' if frozen['ok'] else 'DIVERGENTE'}")
    print(f"triagem em {out}")
    bad = by_class.get("falha_real", 0) + by_class.get("nao_reexecutado", 0) + by_class.get("ambiente", 0)
    return 1 if bad or (frozen is not None and not frozen["ok"]) else 0


if __name__ == "__main__":
    sys.exit(main())
