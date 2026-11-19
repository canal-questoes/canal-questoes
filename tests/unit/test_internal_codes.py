"""Testes do detector de códigos internos (`tests/helpers/internal_codes.py`).

O detector é código de teste e nasce real, então todos estes casos passam desde o esqueleto (higiene; nenhum
depende de implementação). Cobrem: cada tipo de código, cada referência privada e cada caminho privado acusado; os
falsos que não casam; a curva elíptica casada e aceita por uma entrada; os nomes de arquivo, função, classe, constante,
importação e título de teste; a lista de códigos aceitos (formato, entrada sem uso, só a coluna do texto aceito fora
da varredura, motivo com código acusado); as zonas; a regra de binário; e o próprio módulo do detector sem achado.
Cobrem também as formas de referência ao processo acrescentadas depois (os rótulos de achado, as marcas do vermelho,
os rótulos das reaberturas, as revisões por extenso e as citações numeradas), cada uma com os usos do produto, dos
dados de teste e do método publicado que não casam.

Os controles positivos são montados em tempo de execução (por exemplo, a letra, o hífen e os dígitos em pedaços),
para este arquivo não conter nenhum código: ele também passa pela varredura do teste da árvore pública.
"""

from __future__ import annotations

import pytest

from tests.helpers import internal_codes as ic
from tests.helpers.ops import REPO

DETECTOR_REL = "tests/helpers/internal_codes.py"
DOC = "docs/guia.md"


def _join(*parts: str) -> str:
    """Monta um controle positivo em tempo de execução."""
    return "".join(parts)


def _kinds(path: str, text: str) -> list[tuple[str, str]]:
    return [(occurrence.kind, occurrence.text) for occurrence in ic.find_in_text(path, text)]


# Os controles positivos de cada tipo, montados em pedaços.
CODES = (
    _join("D", "-", "72"),
    _join("P", "-", "1", "000"),
    _join("M", "-", "3"),
    _join("S", "-", "12"),
    _join("F", "R-", "045"),
    _join("S", "C-", "001"),
    _join("U", "S", "1"),
    _join("U", "S", "1", "-", "3"),
    _join("T", "0", "13"),
    _join("R", "0"),
    _join("R", "7"),
    _join("R", "1", "0"),
    _join("R", "8", "-COD-", "001"),
    _join("ro", "dada", " 2"),
    _join("Ro", "dadas", " 10"),
)
SECTION = "§"
PRIVATE_REFS = (
    _join("data-", "model ", SECTION),
    _join("contr", "acts", SECTION),
    _join("des", "ign ", SECTION),
    _join("dese", "nho ", SECTION),
    _join("rote", "iro ", SECTION),
    _join("pl", "an.md ", SECTION),
    _join("sp", "ec ", SECTION),
    _join("rese", "arch ", SECTION),
    _join("ta", "sks", ".md"),
    _join("pl", "an", ".md"),
    _join("rese", "arch", ".md"),
    _join("sp", "ec", ".md"),
    _join("data-", "model", ".md"),
    _join("quick", "start", ".md"),
    _join("R", "-", "04"),
    _join("R", "-", "04", ".6"),
    _join("diverg", "ê", "ncia (b)"),
    _join("diverg", "e", "ncia (c)"),
    _join("P", "C", "3"),
    _join("E", "1", "-", "2"),
    _join("gru", "po S", "3"),
    _join("gru", "pos S", "8"),
    _join(SECTION, "4"),
    _join(SECTION, " 11"),
)
PRIVATE_PATHS = (
    _join("docs/", "base-de-", "conhecimento/"),
    _join("docs/", "exec", "ucao/"),
    _join("docs/", "mono", "grafia/"),
    _join("docs/", "arqui", "tetura/"),
    _join("spe", "cs/"),
    _join(".spe", "cify/"),
    _join(".cla", "ude/"),
    _join("spi", "kes/"),
)


# --------------------------------------------------------------------------------------------------
# Conteúdo
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", CODES)
def test_each_code_is_found(code: str) -> None:
    """Cada tipo de código, isolado numa frase, sai como `code` com o texto casado e a linha certa."""
    text = f"primeira linha\nveja ({code}) e siga.\n"
    found = ic.find_in_text(DOC, text)
    assert [(item.path, item.line, item.kind, item.text) for item in found] == [(DOC, 2, "code", code)]


@pytest.mark.parametrize("reference", PRIVATE_REFS)
def test_each_private_reference_is_found(reference: str) -> None:
    """Cada referência a item de documento privado sai como `private_ref`, com o trecho casado no texto."""
    found = ic.find_in_text(DOC, f"como diz {reference} neste ponto")
    assert found and {item.kind for item in found} == {"private_ref"}
    assert any(reference.startswith(item.text) or item.text == reference for item in found)


def test_research_item_reference_is_found() -> None:
    """O item da pesquisa citado com o nome do documento casa como referência privada."""
    reference = _join("rese", "arch ", "R", "-", "10")
    found = _kinds(DOC, f"ver {reference}.")
    assert ("private_ref", reference) in found


# --------------------------------------------------------------------------------------------------
# As formas de referência ao processo que o detector passou a acusar
# --------------------------------------------------------------------------------------------------

# Os códigos: os requisitos e critérios com letra no fim, as revisões numeradas e as revisões por extenso a partir da
# segunda, inclusive a rodada da revisão.
EXTRA_CODES = (
    _join("F", "R-", "046", "b"),
    _join("S", "C-", "002", "a"),
    _join("re", "visão ", "2"),
    _join("Re", "visões ", "10"),
    _join("se", "gunda ", "revisão"),
    _join("Sé", "timas ", "revisões"),
    _join("qua", "rta rodada da ", "revisão"),
    _join("dé", "cima rodada de ", "revisao"),
)

# As referências: o texto de entrada e o trecho casado, montados em pedaços.
EXTRA_PRIVATE_REFS = (
    # os rótulos de achado de revisão, de análise e de verificação
    (_join("B", "-", "07"), _join("B", "-", "07")),
    (_join("L", "-", "02"), _join("L", "-", "02")),
    (_join("B", "-", "4"), _join("B", "-", "4")),
    (_join("I", "-", "08"), _join("I", "-", "08")),
    (_join("K", "1-L-", "05"), _join("K", "1-L-", "05")),
    (_join("C", "1-", "01"), _join("C", "1-", "01")),
    (_join("T", "2-", "14"), _join("T", "2-", "14")),
    (_join("T", "-PO", "LL-", "002"), _join("T", "-PO", "LL-", "002")),
    (_join("blo", "queio B", "1"), _join("blo", "queio B", "1")),
    (_join("Blo", "queios B", "3"), _join("Blo", "queios B", "3")),
    # as marcas dos motivos do vermelho
    (_join("((", "m", "2) do roteiro"), _join("((", "m", "2)")),
    (_join("mo", "tivo m", "4"), _join("mo", "tivo m", "4")),
    (_join("Hi", "giene (h", "2)"), _join("Hi", "giene (h", "2")),
    (_join("(h", "3; verde"), _join("(h", "3;")),
    (_join("ver", "melho por (m", "1)"), _join("por (m", "1)")),
    # os rótulos das mudanças que não são só acréscimo e das reaberturas
    (_join("não adi", "tiva ", "2"), _join("não adi", "tiva ", "2")),
    (_join("não adi", "tiva N", "1"), _join("não adi", "tiva N", "1")),
    (_join("rea", "bertura N", "4"), _join("rea", "bertura N", "4")),
    (_join("rea", "bertura ", "1b"), _join("rea", "bertura ", "1b")),
    # os números de cenário (com a parte decimal, um intervalo ou uma lista) e as clarificações citadas
    (_join("cená", "rio ", "4.1"), _join("cená", "rio ", "4.1")),
    (_join("Cená", "rios ", "1 a 5"), _join("Cená", "rios ", "1 a 5")),
    (_join("cena", "rios ", "2, 3"), _join("cena", "rios ", "2, 3")),
    (_join("Clari", "ficação Q", "1"), _join("Clari", "ficação Q", "1")),
    (_join("clari", "ficação \"troca de token\""), _join("clari", "ficação \"")),
    # os passos e o jargão do roteiro privado
    (_join("pré-", "revisão"), _join("pré-", "revis")),
    (_join("Pre-", "revisao"), _join("Pre-", "revis")),
    (_join("pelo mo", "tivo certo"), _join("pelo mo", "tivo certo")),
    (_join("verdes por con", "strução"), _join("verdes por con", "struç")),
    (_join("Verde por con", "strucao"), _join("Verde por con", "struc")),
    (_join("par ver", "melho"), _join("par ver", "melho")),
    (_join("pares ver", "melhos"), _join("pares ver", "melhos")),
    (_join("vermelho ad", "mitido"), _join("vermelho ad", "mitido")),
    (_join("Indep", "endent Test"), _join("Indep", "endent Test")),
    (_join("exten", "são aditiva"), _join("exten", "são aditiva")),
    (_join("Exten", "sões aditivas"), _join("Exten", "sões aditivas")),
    (_join("tabe", "la (B)"), _join("tabe", "la (B)")),
    # as notas de história em itálico (com a data ou o evento do processo) e os artigos do documento de princípios
    (_join("*(", "Até 2026-10-19: só o texto"), _join("*(", "Até 2026-10-19")),
    (_join("*(", "Até a reabertura da segunda"), _join("*(", "Até a reabert", "ura")),
    (_join("*(", "Ate a revisão do teste"), _join("*(", "Ate a revis")),
    (_join("constitu", "ição III"), _join("constitu", "ição III")),
    (_join("Constitu", "ição (V)"), _join("Constitu", "ição (V")),
)


@pytest.mark.parametrize("code", EXTRA_CODES)
def test_each_extra_code_is_found(code: str) -> None:
    """Os requisitos e critérios com letra no fim, as revisões numeradas e as revisões por extenso a partir da segunda
    saem como `code`, uma vez só, com o texto casado e a linha certa."""
    text = f"primeira linha\nveja ({code}) e siga.\n"
    found = ic.find_in_text(DOC, text)
    assert [(item.path, item.line, item.kind, item.text) for item in found] == [(DOC, 2, "code", code)]


@pytest.mark.parametrize(("text", "matched"), EXTRA_PRIVATE_REFS)
def test_each_extra_private_reference_is_found(text: str, matched: str) -> None:
    """Cada forma nova de referência ao processo sai como `private_ref`, uma vez só, com o trecho casado."""
    assert _kinds(DOC, f"como diz {text} neste ponto") == [("private_ref", matched)]


@pytest.mark.parametrize("text", (
    # as rodadas de uma tarefa do produto, mesmo por extenso, e a reabertura de uma entrega
    "a primeira rodada do update termina sem efeito",
    "já na primeira rodada da regra de aviso",
    "a primeira rodada é `r1`",
    "a segunda rodada da tarefa falha de novo",
    "na terceira rodada failed o teto esgota",
    "a primeira revisão dos testes",
    "a entrega é reaberta e a reabertura pede o campo de novo",
    "avanço ou reabertura do campo",
    # os alunos neutros, as sementes e os rótulos dos dados de teste
    "\"N1\": _Pupil(\"N1\", NEUTRAL[0]), \"N2\": _Pupil(\"N2\", NEUTRAL[1])",
    "o neutro N1 marca (a) e o N2 marca (b)",
    "o neutro (N1) marca e o (N2) não",
    "o aluno A-2 e a turma K-12",
    "os N-1 primeiros",
    "as sementes O2 e O3",
    "A1/B1",
    "de D0 a D3",
    "a T1 de B revoga A1 e gera B1",
    "HIDDEN_KEY = \"GABARITO-OCULTO-B2\"",
    # os nomes de mídia e as variáveis numeradas
    "a opção reusa true.png (m3)",
    "record = outbox.get(m1)",
    "for m in (m1, m2, m3):",
    "assert rows == [(m1, text, 1), (m2, other, 2)]",
    "cq-media:m3",
    "O transporte (h11) recusa o valor",
    # as regras do lint, as codificações e os cabeçalhos
    "# noqa: B018 - valida a porta",
    "# noqa: B023",
    "E501",
    "UTF-8",
    "SHA-1",
    "ISO-8601",
    "AES-256",
    "X-Request-Id",
    "HTTP(S)_PROXY",
    "Copyright (C) 2007",
    # o vocabulário do método de desenvolvimento, que é publicado
    "revisão adversarial dupla",
    "a revisão humana adiada",
    "a convergência do código com a especificação",
    "a reabertura dos testes congelados",
    "uma rodada de revisão",
    "a próxima fatia do roteiro",
    "a fase de testes",
    "o Codex e o revisor Claude",
    # palavras do produto e dos guias
    "o plano de disparos",
    "o contrato do motor",
    "uma tabela de eventos",
    "a extensão do arquivo",
    "o cenário de várias partes",
    "no cenário 2 do piloto",
    "*(Depois de subir a pilha)*",
    "*(Antes de tudo)*",
    "*(Até 20 alunos)*",
    "a constituição do projeto",
    "lê a constituição no repositório privado",
    "até 2026-11-20",
    "(antes do envio)",
    "o motivo mais comum",
    "pelo motivo do erro",
))
def test_product_and_method_uses_are_not_found(text: str) -> None:
    """As formas novas não acusam os usos do produto, dos dados de teste, dos guias e do método publicado: as rodadas
    de uma tarefa, mesmo por extenso, a reabertura de uma entrega, os alunos neutros, com ou sem parênteses, os
    rótulos de aluno e as contas com hífen, as sementes, os nomes de mídia, as variáveis numeradas, as regras do lint,
    as codificações, as notas em itálico de um guia e o vocabulário da revisão, da convergência e das fatias."""
    assert ic.find_in_text(DOC, text) == []


@pytest.mark.parametrize("text", (*EXTRA_CODES, *(text for text, _ in EXTRA_PRIVATE_REFS)))
def test_ts_titles_with_the_extra_references_are_names(text: str) -> None:
    """Num `.ts` da zona dos testes, um título com qualquer forma nova de referência sai de `find_in_names` como
    `name`, com a linha do título, e fica fora do conteúdo; o título sem referência não sai."""
    source = (f"test('caso do gabarito ({text})', () => {{}});\n"
              "it('sem rótulo nenhum', () => {});\n")
    path = "engine/test/integration/x.test.ts"
    assert [(item.line, item.kind) for item in ic.find_in_names(path, source)] == [(1, "name")]
    assert ic.find_in_text(path, source) == []


@pytest.mark.parametrize("path", PRIVATE_PATHS)
@pytest.mark.parametrize("before", ("", " ", "`", '"', "(", "/", "../"))
def test_each_private_path_is_found(path: str, before: str) -> None:
    """Cada caminho privado sai como `private_path`, no começo da linha ou depois de um separador, e o texto é o
    caminho sem o caractere que o antecede."""
    assert _kinds(DOC, f"{before}{path}arquivo.md") == [("private_path", path)]


@pytest.mark.parametrize("path", PRIVATE_PATHS)
@pytest.mark.parametrize("before", ("a", "Z", "0", "_", "-", "."))
def test_private_path_inside_a_word_is_not_found(path: str, before: str) -> None:
    """Um caminho privado colado a uma letra, um dígito, `_`, `-` ou `.` não casa (é outro nome)."""
    assert [kind for kind, _ in _kinds(DOC, f"{before}{path}arquivo.md") if kind == "private_path"] == []


@pytest.mark.parametrize("text", (
    "UTF-8", "SHA-256", "CVE-2024-1234", "sha-1", "r0 = 1", "r7.append(x)", ".claude-plugin/plugin.json", "S3",
    "o bucket S3 da AWS", "xD-12", "D-12a", "T01", "T0134", "FR-04", "R11", "AR9", "R9x", "US", "rodada", "PC10",
))
def test_known_false_positives_are_not_found(text: str) -> None:
    """Os falsos conhecidos não casam: a codificação, o resumo criptográfico, os identificadores de vulnerabilidade,
    as variáveis locais numeradas em minúsculas, a pasta do plugin, um nome de serviço sem a palavra "grupo" e os
    códigos colados ou com o número fora da forma."""
    assert ic.find_in_text(DOC, text) == []


def test_elliptic_curve_matches_and_an_entry_accepts_it() -> None:
    """A curva do certificado de teste casa o padrão de código; uma entrada da lista de códigos aceitos a aceita no
    arquivo da entrada e só nele."""
    curve = _join("P", "-", "256")
    text = f"certificado de teste (EC {curve}, SHA-256)\n"
    found = ic.find_in_text("tests/helpers/smtp.py", text)
    assert [(item.kind, item.text) for item in found] == [("code", curve)]
    entries = ic.load_accepted(f"tests/helpers/smtp.py\t{curve}\tcurva elíptica do certificado de teste\n")
    remaining, unused = ic.apply_accepted(found, entries)
    assert remaining == [] and unused == []
    other = ic.find_in_text("tests/helpers/outro.py", text)
    remaining, unused = ic.apply_accepted(other, entries)
    assert remaining == other and unused == entries


def test_several_occurrences_keep_line_and_order() -> None:
    """Várias ocorrências: cada uma com a sua linha (a primeira é 1), na ordem do texto; o CRLF não muda a linha."""
    first, second = _join("D", "-", "15"), _join("F", "R-", "001")
    path = _join("spe", "cs/")
    text = f"{first} e {second}\r\nnada aqui\r\nleia {path}x\r\n"
    found = ic.find_in_text(DOC, text)
    assert [(item.line, item.kind, item.text) for item in found] == [
        (1, "code", first), (1, "code", second), (3, "private_path", path)]


def test_occurrence_fields() -> None:
    """A ocorrência tem caminho, linha, tipo e texto, e é imutável."""
    found = ic.find_in_text("a/b.py", _join("# ", "T", "0", "42"))
    assert found == [ic.Occurrence("a/b.py", 1, "code", _join("T", "0", "42"))]
    with pytest.raises(AttributeError):
        found[0].line = 2  # type: ignore[misc]


# --------------------------------------------------------------------------------------------------
# Títulos do TypeScript
# --------------------------------------------------------------------------------------------------


def _ts_source() -> str:
    code = _join("R", "1")
    reference = _join("con", "tracts ", SECTION, "2")
    return (
        "import { test } from 'node:test';\n"
        f"describe('suíte do {code}', () => {{\n"
        f"  it(\"caso da {reference}\", () => {{}});\n"
        "  test.skip(`sem código`, () => {});\n"
        f"  t.test('subteste {code}', () => {{}});\n"
        f"  // comentário do {code}\n"
        "});\n"
        f"test(\n  'quebra de linha {code}',\n  () => {{}});\n"
    )


def test_ts_titles_are_names_not_content() -> None:
    """Nos `.ts` da zona dos testes, os títulos de `describe`, `it`, `test` (com `t.` e `.skip`) ficam fora do
    conteúdo; o comentário continua conteúdo."""
    source = _ts_source()
    found = ic.find_in_text("engine/test/unit/x.test.ts", source)
    assert [(item.line, item.kind) for item in found] == [(6, "code")]


def test_ts_titles_with_codes_are_name_occurrences() -> None:
    """Os títulos com código ou referência privada saem de `find_in_names` como `name`, com a linha do título; o
    título sem código não sai."""
    source = _ts_source()
    found = ic.find_in_names("engine/test/unit/x.test.ts", source)
    assert [(item.line, item.kind) for item in found] == [(2, "name"), (3, "name"), (5, "name"), (9, "name")]
    assert all(item.text[0] in "'\"`" for item in found)


def test_ts_titles_outside_the_test_zone_are_content() -> None:
    """Fora da zona dos testes, um `.ts` não tem títulos: tudo é conteúdo, e `find_in_names` não acha nome."""
    source = _ts_source()
    found = ic.find_in_text("engine/src/x.ts", source)
    assert len(found) == 6
    assert ic.find_in_names("engine/src/x.ts", source) == []


def test_ts_method_named_like_a_runner_is_content() -> None:
    """Um método com o mesmo final (`split`, `obj.digit`) não é título de teste."""
    code = _join("R", "2")
    source = f"const a = linha.split('{code}');\n"
    assert [item.kind for item in ic.find_in_text("engine/test/unit/y.test.ts", source)] == ["code"]
    assert ic.find_in_names("engine/test/unit/y.test.ts", source) == []


# --------------------------------------------------------------------------------------------------
# Nomes e caminhos
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("segment", (
    _join("test_canary_", "r", "9", ".py"), _join("r", "10"), _join("bot_api_", "r", "5"), _join("us", "3", "_x"),
    _join("t", "013", ".md"), _join("x-", "fr", "045"), _join("sc", "001", ".json"), _join("d", "72"),
    _join("p", "999", "_y"), _join("s", "01"), _join("m", "01"), _join("TEST_", "R", "9", ".py"),
    _join("x.", "R", "1", "0", ".ts"),
))
def test_slice_code_in_path_segment_is_found(segment: str) -> None:
    """Um segmento de caminho com código de fatia (sem caixa, entre separadores) sai como `name`, linha 0, com o
    segmento como texto."""
    path = f"tests/unit/{segment}/arquivo.txt"
    assert ic.find_in_path(path) == [ic.Occurrence(path, 0, "name", segment)]


@pytest.mark.parametrize("path", (
    "tests/unit/test_export_public.py", "engine/test/fixtures/error0.json", "docs/r100/x.md", "src/sha256.py",
    "src/utf8.py", "src/mirror7/x.py", "src/t01/x.py", "src/d1/x.py", "src/s1/x.py", "src/us/x.py",
))
def test_paths_without_slice_code_are_clean(path: str) -> None:
    """Caminhos sem código de fatia não acusam nada (o código precisa de separador nas duas pontas)."""
    assert ic.find_in_path(path) == []


def _python_source() -> str:
    r9, r5, r3, d72, us2 = (_join("r", "9"), _join("r", "5"), _join("R", "3"), _join("d", "72"), _join("us", "2"))
    return (
        f"import tests.helpers.bot_api_{r5}\n"
        f"from tests.helpers.cli_{r9} import Cli as Cli_{r9}\n"
        f"from x import y as y_{us2}\n"
        f"LIMIT_{r3} = 3\n"
        f"plain, other_{d72} = 1, 2\n"
        "if True:\n"
        f"    FLAG_{r9} = True\n"
        f"class Fake_{r3}:\n"
        f"    attr_{r9}: int = 1\n"
        f"    def method_{r5}(self):\n"
        f"        local_{r9} = 1\n"
        f"        return local_{r9}\n"
        f"def test_case_{r9}():\n"
        f"    {r9} = 2\n"
        f"    def inner_{r5}():\n"
        f"        pass\n"
        f"    return {r9}\n"
        f"async def fetch_{r5}():\n"
        f"    pass\n"
        "def clean():\n"
        "    r0 = 1\n"
        "    return r0\n"
    )


def test_python_defined_names_with_slice_codes() -> None:
    """Python: os nomes de `def`, `async def`, `class`, as atribuições do módulo e da classe (inclusive dentro de um
    `if` do módulo) e os nomes importados (o módulo e o nome ligado) saem como `name`, com a linha; as variáveis locais
    ficam fora."""
    r9, r5, r3, d72, us2 = (_join("r", "9"), _join("r", "5"), _join("R", "3"), _join("d", "72"), _join("us", "2"))
    found = ic.find_in_names("tests/unit/x.py", _python_source())
    assert all(item.kind == "name" and item.path == "tests/unit/x.py" for item in found)
    assert sorted((item.line, item.text) for item in found) == sorted([
        (1, f"tests.helpers.bot_api_{r5}"),
        (2, f"tests.helpers.cli_{r9}"),
        (2, f"Cli_{r9}"),
        (3, f"y_{us2}"),
        (4, f"LIMIT_{r3}"),
        (5, f"other_{d72}"),
        (7, f"FLAG_{r9}"),
        (8, f"Fake_{r3}"),
        (9, f"attr_{r9}"),
        (10, f"method_{r5}"),
        (13, f"test_case_{r9}"),
        (15, f"inner_{r5}"),
        (18, f"fetch_{r5}"),
    ])


def test_python_names_do_not_count_content() -> None:
    """`find_in_names` não olha o conteúdo: um código em comentário ou string não é nome."""
    code = _join("R", "9")
    assert ic.find_in_names("a.py", f"# {code}\nX = '{code}'\n") == []


def test_other_files_have_no_names() -> None:
    """Arquivos que não são Python nem `.ts` de teste não têm nomes a conferir."""
    assert ic.find_in_names("docs/x.md", _join("def test_", "r", "9", "():\n")) == []


# --------------------------------------------------------------------------------------------------
# Lista de códigos aceitos
# --------------------------------------------------------------------------------------------------


def test_load_accepted_reads_entries_and_skips_comments() -> None:
    """A lista: `<caminho>TAB<texto>TAB<motivo>`, comentários e linhas vazias ignorados, na ordem."""
    path_text = _join(".cla", "ude/")
    text = ("# cabeçalho\n\n"
            f".gitignore\t{path_text}\treferência funcional: o git ignora a pasta\n"
            f"tests/a.py\t{path_text}\treferência funcional: o teste confere que a pasta não sai\n")
    assert ic.load_accepted(text) == [
        ic.AcceptedEntry(".gitignore", path_text, "referência funcional: o git ignora a pasta"),
        ic.AcceptedEntry("tests/a.py", path_text, "referência funcional: o teste confere que a pasta não sai"),
    ]
    assert ic.load_accepted("# só comentário\n") == []


@pytest.mark.parametrize("line", (
    "a.py\tsó dois campos",
    "a.py\ttexto\tmotivo\tsobra",
    "a.py\t\tmotivo",
    "\ttexto\tmotivo",
    "a.py\ttexto\t",
    " a.py\ttexto\tmotivo",
    "a.py\t texto\tmotivo",
    "tests/*.py\ttexto\tmotivo",
    "tests/a?.py\ttexto\tmotivo",
    "/abs/a.py\ttexto\tmotivo",
    "tests\\a.py\ttexto\tmotivo",
    "tests/../a.py\ttexto\tmotivo",
    "tests//a.py\ttexto\tmotivo",
))
def test_load_accepted_refuses_bad_lines_without_echo(line: str) -> None:
    """Linha fora da forma → `ListInvalid` (um `ValueError`), com a mensagem fixa, sem eco da linha."""
    with pytest.raises(ic.ListInvalid) as caught:
        ic.load_accepted(f"# cabeçalho\n{line}\n")
    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == "lista inválida"
    assert "texto" not in str(caught.value) and "a.py" not in str(caught.value)


def test_load_accepted_refuses_crlf_and_repeated_pairs() -> None:
    """CRLF e o mesmo par caminho e texto repetido (mesmo com outro motivo) → `ListInvalid`."""
    with pytest.raises(ic.ListInvalid):
        ic.load_accepted("a.py\ttexto\tmotivo\r\n")
    with pytest.raises(ic.ListInvalid):
        ic.load_accepted("a.py\ttexto\tmotivo um\na.py\ttexto\tmotivo dois\n")
    assert len(ic.load_accepted("a.py\ttexto\tmotivo\nb.py\ttexto\tmotivo\n")) == 2


def test_apply_accepted_matches_path_and_text_and_reports_unused() -> None:
    """Uma entrada aceita toda ocorrência do mesmo texto no mesmo arquivo (comentário incluído); o que sobra mantém a
    ordem; a entrada sem uso é devolvida."""
    private = _join("spe", "cs/")
    other_private = _join(".spe", "cify/")
    text = f"ROOT = '{private}x'\n# a pasta {private} não sai\nOUTRA = '{other_private}y'\n"
    found = ic.find_in_text("tests/a.py", text)
    assert [(item.line, item.text) for item in found] == [(1, private), (2, private), (3, other_private)]
    entries = [ic.AcceptedEntry("tests/a.py", private, "referência funcional"),
               ic.AcceptedEntry("tests/b.py", other_private, "referência funcional"),
               ic.AcceptedEntry("tests/a.py", _join("P", "-", "256"), "curva")]
    remaining, unused = ic.apply_accepted(found, entries)
    assert remaining == [found[2]]
    assert unused == entries[1:]


def test_accepted_list_only_skips_the_accepted_text_column() -> None:
    """Na própria lista de códigos aceitos, só a coluna do texto aceito fica fora da varredura: um código no motivo,
    num comentário ou no caminho é acusado; o texto aceito, não."""
    code = _join("D", "-", "99")
    private = _join(".cla", "ude/")
    text = ("# cabeçalho sem código\n"
            f"tests/a.py\t{private}\treferência funcional\n"
            f"tests/b.py\t{code}\tmotivo limpo\n"
            f"tests/c.py\t{private}\tmotivo com {code}\n"
            f"# comentário com {code}\n")
    found = ic.find_in_text(ic.ACCEPTED_CODES_PATH, text)
    assert [(item.line, item.kind, item.text) for item in found] == [(4, "code", code), (5, "code", code)]
    # O mesmo texto em outro arquivo é varrido inteiro.
    assert len(ic.find_in_text("ops/export/outra-lista.txt", text)) == 5
    with_path = f"docs/{_join('R', '1')}/x.md\ttexto\tmotivo\n"
    assert [item.kind for item in ic.find_in_text(ic.ACCEPTED_CODES_PATH, with_path)] == ["code"]


def test_accepted_codes_path_is_the_list_in_the_tree() -> None:
    """O caminho da lista é o de `ops/export/`, o mesmo que o teste das listas e o da árvore pública leem."""
    assert ic.ACCEPTED_CODES_PATH == "ops/export/accepted-codes.txt"
    assert (REPO / ic.ACCEPTED_CODES_PATH).is_file()


# --------------------------------------------------------------------------------------------------
# Zonas, binários e o próprio módulo
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "zone"), (
    ("tests/unit/test_x.py", "tests"),
    ("tests/fixtures/repos/course/info.json", "tests"),
    ("engine/test/unit/a.test.ts", "tests"),
    ("engine/test/fixtures/x.json", "tests"),
    ("engine/conformance/oracle.ts", "tests"),
    ("engine/conformance/resultados/x.json", "rest"),
    ("engine/src/index.ts", "rest"),
    ("server/src/cq_server/app.py", "rest"),
    ("ops/export/accepted-codes.txt", "rest"),
    ("testsx/a.py", "rest"),
    ("README.md", "rest"),
))
def test_zones(path: str, zone: str) -> None:
    """A zona dos testes: `tests/`, `engine/test/` e os `.ts` de `engine/conformance/`; o resto é `rest`."""
    assert ic.zone_of(path) == zone


@pytest.mark.parametrize(("data", "binary"), (
    (b"texto comum\n", False),
    ("acentuação".encode("utf-8"), False),
    (b"\x89PNG\r\n\x1a\n\0\0", True),
    (b"\xff\xfe latin", True),
    (b"a" * 9000 + b"\0", False),
))
def test_binary_rule(data: bytes, binary: bool) -> None:
    """Binário pela regra da varredura: NUL nos primeiros 8 KiB ou UTF-8 inválido."""
    assert ic.is_binary(data) is binary


def test_detector_module_has_no_finding() -> None:
    """O próprio módulo do detector passa pela própria varredura sem achado: conteúdo, nomes e caminho."""
    source = (REPO / DETECTOR_REL).read_text(encoding="utf-8")
    assert ic.find_in_text(DETECTOR_REL, source) == []
    assert ic.find_in_names(DETECTOR_REL, source) == []
    assert ic.find_in_path(DETECTOR_REL) == []


def test_this_test_module_has_no_finding() -> None:
    """Este arquivo de teste também não tem código interno: os controles são montados em tempo de execução."""
    rel = "tests/unit/test_internal_codes.py"
    source = (REPO / rel).read_text(encoding="utf-8")
    assert ic.find_in_text(rel, source) == []
    assert ic.find_in_names(rel, source) == []
    assert ic.find_in_path(rel) == []
