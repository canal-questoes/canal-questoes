// Checagem independente do subconjunto canônico de HTML.
import assert from 'node:assert/strict';

import * as cheerio from 'cheerio';

const ALLOWED: Record<string, string[]> = {
  p: [], br: [], h1: [], h2: [], h3: [], h4: [], h5: [], h6: [],
  strong: [], em: [], u: [], s: [], sup: [], sub: [], blockquote: [],
  ul: [], ol: [], li: [],
  table: [], thead: [], tbody: [], tr: [], th: ['colspan', 'rowspan'], td: ['colspan', 'rowspan'],
  pre: [], code: ['class'], img: ['src', 'alt'], 'cq-math': ['display'], 'cq-field': ['name'],
};

export function assertCanonicalHtml(html: string): void {
  assert.equal(typeof html, 'string', 'HTML canônico deve ser texto');
  assert.ok(!html.includes('<!--'), 'comentário HTML no subconjunto canônico');
  const $ = cheerio.load(html, null, false);
  $('*').each((_, el) => {
    if (el.type !== 'tag') return;
    const tag = el.name.toLowerCase();
    assert.ok(!tag.startsWith('pl-') && !tag.startsWith('pl_'), `tag de elemento do PL no HTML canônico: <${tag}>`);
    assert.ok(tag in ALLOWED, `tag fora do subconjunto canônico: <${tag}>`);
    for (const [attr, value] of Object.entries(el.attribs ?? {})) {
      assert.ok(ALLOWED[tag].includes(attr), `atributo "${attr}" não permitido em <${tag}>`);
      if (tag === 'code' && attr === 'class') {
        assert.match(value, /^language-[A-Za-z0-9_+#.-]+$/, `class de <code> fora de language-*: ${value}`);
      }
      if (tag === 'img' && attr === 'src') {
        assert.match(value, /^cq-media:m[0-9]+$/, `src de <img> fora de cq-media:mN: ${value}`);
      }
      if (tag === 'cq-math' && attr === 'display') {
        assert.ok(value === 'inline' || value === 'block', `display de cq-math inválido: ${value}`);
      }
    }
    if (tag === 'cq-math') {
      assert.ok(el.attribs?.display, 'cq-math sem display');
      assert.equal($(el).children().length, 0, 'cq-math com marcação interna');
    }
    if (tag === 'cq-field') {
      assert.ok(el.attribs?.name, 'cq-field sem name');
      assert.equal($(el).contents().length, 0, 'cq-field com conteúdo');
    }
  });
}
