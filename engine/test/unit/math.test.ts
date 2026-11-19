// convertMath: delimitadores de fórmula → <cq-math display="…">.
// Cobre as fórmulas (as escritas com os delimitadores do PL viram fórmulas marcadas, com o LaTeX preservado)
// e as regras do HTML canônico: $…$ e \(…\) inline; $$…$$ e \[…\] block;
// \$ é cifrão literal; nada dentro de code/pre nem de elementos com classe mathjax_ignore ou
// tex2jax_ignore ([F] ignoreHtmlClass de src/lib/client/mathjax.ts do PL); o texto decodificado do
// cq-math é o LaTeX exato, sem colapso de espaços. Toda comparação usa o texto decodificado via cheerio.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import * as cheerio from 'cheerio';

import { convertMath } from '../../src/ir/math.ts';

interface MathNode {
  display: string | undefined;
  text: string;
}

function parse(html: string): cheerio.CheerioAPI {
  return cheerio.load(html, null, false);
}

/** Os cq-math do HTML, na ordem do documento, com o texto decodificado. */
function maths(html: string): MathNode[] {
  const $ = parse(html);
  return $('cq-math')
    .toArray()
    .map((el) => ({ display: $(el).attr('display'), text: $(el).text() }));
}

/** Texto decodificado do fragmento inteiro. */
function textOf(html: string): string {
  return parse(html).root().text();
}

describe('convertMath', () => {
  test('$…$ e \\(…\\) viram cq-math inline', () => {
    const out = convertMath('<p>seja $x^2$ e \\(y\\)</p>');
    assert.deepEqual(maths(out), [
      { display: 'inline', text: 'x^2' },
      { display: 'inline', text: 'y' },
    ]);
    assert.equal(parse(out)('p').text(), 'seja x^2 e y', 'o texto em volta fica, e os delimitadores saem');
  });

  test('$$…$$ e \\[…\\] viram cq-math block', () => {
    assert.deepEqual(maths(convertMath('$$\\int f$$')), [{ display: 'block', text: '\\int f' }]);
    assert.deepEqual(maths(convertMath('\\[a+b\\]')), [{ display: 'block', text: 'a+b' }]);
  });

  test('o LaTeX do bloco é preservado byte a byte, sem colapso de espaços', () => {
    const out = convertMath('$$a %c\n+b$$');
    const found = maths(out);
    assert.equal(found.length, 1);
    assert.equal(found[0].display, 'block');
    assert.equal(found[0].text, 'a %c\n+b');
  });

  test('\\$ vira cifrão literal, sem cq-math', () => {
    const out = convertMath('custa \\$5');
    assert.deepEqual(maths(out), []);
    assert.equal(textOf(out), 'custa $5');
  });

  test('code, pre, mathjax_ignore e tex2jax_ignore ficam intactos', () => {
    const cases: Array<[string, string, string]> = [
      ['<code>$x$</code>', 'code', '$x$'],
      ['<pre>$y$</pre>', 'pre', '$y$'],
      ['<span class="mathjax_ignore">$z$</span>', 'span.mathjax_ignore', '$z$'],
      ['<span class="tex2jax_ignore">$w$</span>', 'span.tex2jax_ignore', '$w$'],
    ];
    for (const [input, selector, text] of cases) {
      const out = convertMath(input);
      assert.deepEqual(maths(out), [], `não deveria haver cq-math em ${input}`);
      assert.equal(parse(out)(selector).text(), text, `conteúdo alterado em ${input}`);
    }
  });

  test('só o texto fora das regiões ignoradas é convertido', () => {
    const out = convertMath('<p>$a$ <code>$x$</code> <span class="mathjax_ignore">$z$</span></p>');
    assert.deepEqual(maths(out), [{ display: 'inline', text: 'a' }]);
    const $ = parse(out);
    assert.equal($('code').text(), '$x$');
    assert.equal($('span.mathjax_ignore').text(), '$z$');
  });

  test('$ sem par fica literal', () => {
    const out = convertMath('<p>um $ sozinho</p>');
    assert.deepEqual(maths(out), []);
    assert.equal(textOf(out), 'um $ sozinho');
  });

  test('"$a &lt; b$": o texto decodificado é "a < b" e a serialização escapa o <', () => {
    const out = convertMath('$a &lt; b$');
    assert.deepEqual(maths(out), [{ display: 'inline', text: 'a < b' }]);
    assert.ok(out.includes('a &lt; b'), `a string serializada deveria conter "a &lt; b": ${out}`);
  });

  test('a fórmula que atravessa <em> não é convertida', () => {
    const out = convertMath('<p>$a <em>b</em> c$</p>');
    assert.deepEqual(maths(out), []);
    assert.equal(textOf(out), '$a b c$');
  });
});
