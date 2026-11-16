// canonicalizeHtml: subconjunto canônico de HTML.
// Cobre o enunciado restrito ao subconjunto canônico (marcação fora dele removida, preservando o
// texto; campos como marcadores), a imagem que referencia uma mídia, a canonicalização em
// TypeScript e as regras do HTML canônico: trocas b/i/ins/del/strike, desembrulho, remoção com conteúdo,
// pre literal, conteúdo escondido por CSS (visually-hidden, sr-only e a coluna td.linenos da
// table.highlighttable do pl-code — [F] pl-code.py linha 192, linenos="table"; pl-checkbox.mustache
// usa <legend class="visually-hidden">Checkbox options</legend>), code language-*, img cq-media,
// espaços e p vazio. Toda saída passa em assertCanonicalHtml.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import * as cheerio from 'cheerio';

import { canonicalizeHtml } from '../../src/ir/canonical-html.ts';
import { assertCanonicalHtml } from '../helpers/canonical-html-checker.ts';

type Opts = Parameters<typeof canonicalizeHtml>[1];

/** Canonicaliza e exige que a saída pertença ao subconjunto canônico. */
function canon(html: string, opts?: Opts): string {
  const out = canonicalizeHtml(html, opts);
  assertCanonicalHtml(out);
  return out;
}

function parse(html: string): cheerio.CheerioAPI {
  return cheerio.load(html, null, false);
}

describe('canonicalizeHtml', () => {
  test('b→strong, i→em, ins→u, del→s e strike→s', () => {
    assert.equal(
      canon('<p><b>a</b><i>b</i><ins>c</ins><del>d</del><strike>e</strike></p>'),
      '<p><strong>a</strong><em>b</em><u>c</u><s>d</s><s>e</s></p>',
    );
  });

  test('div e span são desembrulhados, preservando o texto', () => {
    assert.equal(canon('<div><p>x <span>y</span></p></div>'), '<p>x y</p>');
  });

  test('script, style, template, input, button, select, textarea e comentários saem com o conteúdo', () => {
    const out = canon(
      '<p>a<script>SECRETO1()</script><style>.SECRETO2{}</style><template><b>SECRETO3</b></template>' +
        '<input value="SECRETO4"><button>SECRETO5</button><select><option>SECRETO6</option></select>' +
        '<textarea>SECRETO7</textarea><!-- SECRETO8 -->b</p>',
    );
    assert.ok(!out.includes('SECRETO'), `conteúdo removido vazou: ${out}`);
    assert.equal(out, '<p>ab</p>');
  });

  test('table preserva colspan e perde class, style e id', () => {
    assert.equal(
      canon(
        '<table class="t" id="x" style="color:red"><tbody><tr>' +
          '<td colspan="2" class="c" style="width:1px" id="y">a</td></tr></tbody></table>',
      ),
      '<table><tbody><tr><td colspan="2">a</td></tr></tbody></table>',
    );
  });

  test('pre com span de realce vira texto literal, com as quebras de linha', () => {
    assert.equal(
      canon('<pre><span class="k">def</span> f():\n    <span class="k">return</span> 1</pre>'),
      '<pre>def f():\n    return 1</pre>',
    );
  });

  test('table.highlighttable do pl-code vira só o pre do td.code, sem os números de linha', () => {
    assert.equal(
      canon(
        '<table class="highlighttable"><tr><td class="linenos"><pre>1\n2</pre></td>' +
          '<td class="code"><pre>a\nb</pre></td></tr></table>',
      ),
      '<pre>a\nb</pre>',
    );
  });

  test('saída real do pygments com linenos="table" (formato do pl-code) perde a coluna de números', () => {
    // Gerado no container pl-oracle com HtmlFormatter(nobackground=True, noclasses=True, linenos="table").
    const pygments =
      '<div class="highlight"><table class="highlighttable"><tr><td class="linenos"><div class="linenodiv"><pre>' +
      '<span style="color: inherit; background-color: transparent; padding-left: 5px; padding-right: 5px;">1</span>\n' +
      '<span style="color: inherit; background-color: transparent; padding-left: 5px; padding-right: 5px;">2</span>' +
      '</pre></div></td><td class="code"><div><pre style="line-height: 125%;"><span></span>x ' +
      '<span style="color: #666">=</span> <span style="color: #666">1</span>\ny <span style="color: #666">=</span> ' +
      '<span style="color: #666">2</span>\n</pre></div></td></tr></table></div>';
    assert.equal(canon(pygments), '<pre>x = 1\ny = 2\n</pre>');
  });

  test('visually-hidden e sr-only somem com o conteúdo', () => {
    assert.equal(canon('<p>a<span class="visually-hidden">Checkbox options</span>b</p>'), '<p>ab</p>');
    assert.equal(canon('<p>a<span class="sr-only">x</span>b</p>'), '<p>ab</p>');
    assert.equal(canon('<p>a<span class="small visually-hidden">oculto</span>b</p>'), '<p>ab</p>');
    assert.equal(
      canon('<fieldset class="d-block"><legend class="visually-hidden">Checkbox options</legend><p>x</p></fieldset>'),
      '<p>x</p>',
    );
  });

  test('code class="language-python" é mantido; code class="x" perde o class', () => {
    assert.equal(
      canon('<p><code class="language-python">x = 1</code></p>'),
      '<p><code class="language-python">x = 1</code></p>',
    );
    assert.equal(canon('<p><code class="x">y</code></p>'), '<p><code>y</code></p>');
  });

  test('img com resolveImage → <img src="cq-media:m1" alt="…">', () => {
    const calls: Array<[string, string | null]> = [];
    const out = canon('<p><img src="/pl/clientFilesQuestion/figura.png" alt="Figura estática" class="img-fluid"></p>', {
      resolveImage: (src, alt) => {
        calls.push([src, alt]);
        return 'm1';
      },
    });
    assert.deepEqual(calls, [['/pl/clientFilesQuestion/figura.png', 'Figura estática']]);
    const $ = parse(out);
    const imgs = $('img').toArray();
    assert.equal(imgs.length, 1);
    assert.deepEqual({ ...imgs[0].attribs }, { src: 'cq-media:m1', alt: 'Figura estática' });
  });

  test('img sem alt: resolveImage recebe alt null', () => {
    const calls: Array<[string, string | null]> = [];
    const out = canon('<p><img src="y.png"></p>', {
      resolveImage: (src, alt) => {
        calls.push([src, alt]);
        return 'm2';
      },
    });
    assert.deepEqual(calls, [['y.png', null]]);
    assert.equal(parse(out)('img').attr('src'), 'cq-media:m2');
  });

  test('img com resolveImage null some, e o alt fica como texto', () => {
    const out = canon('<p>antes <img src="ausente.png" alt="Texto alternativo"> depois</p>', {
      resolveImage: () => null,
    });
    const $ = parse(out);
    assert.equal($('img').length, 0, `a imagem deveria sair: ${out}`);
    assert.ok($('p').text().includes('Texto alternativo'), `o alt deveria ficar como texto: ${out}`);
    assert.ok($('p').text().includes('antes') && $('p').text().includes('depois'));
  });

  test('espaços consecutivos colapsam fora de pre e de cq-math', () => {
    assert.equal(canon('<p>a   b\n\n c</p>'), '<p>a b c</p>');
    const out = canon('<p>x    y</p><pre>x    y\n  z</pre>');
    const $ = parse(out);
    assert.equal($('p').text(), 'x y');
    assert.equal($('pre').text(), 'x    y\n  z', 'pre mantém os espaços');
  });

  test('o texto de um cq-math com \\n é preservado', () => {
    const out = canon('<p><cq-math display="block">a %c\n+b</cq-math></p>');
    const $ = parse(out);
    assert.equal($('cq-math').attr('display'), 'block');
    assert.equal($('cq-math').text(), 'a %c\n+b');
  });

  test('p vazio sai', () => {
    assert.equal(canon('<p></p><p>x</p><p></p>'), '<p>x</p>');
  });

  test('cq-field e cq-math são preservados', () => {
    assert.equal(
      canon('<p>Resposta: <cq-field name="num"></cq-field> com <cq-math display="inline">x^2</cq-math></p>'),
      '<p>Resposta: <cq-field name="num"></cq-field> com <cq-math display="inline">x^2</cq-math></p>',
    );
  });
});
