Diretório de `elements/` sem `info.json` (fixture do motor). O PL não o trata como elemento do curso
(`freeform.ts`, `loadElements`, ignora o diretório quando o `info.json` não existe), e por isso a tag
`<pl-code>` continua resolvendo para o `pl-code` core.
