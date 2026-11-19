# Imagem de teste do engine. Congelada com os testes.
#   docker build -f engine/Dockerfile -t canal-questoes/engine:dev .
#   docker build -f engine/test/Dockerfile -t canal-questoes/engine:test .
#   docker run --rm --network none --user 65534:65534 --read-only --tmpfs /tmp canal-questoes/engine:test
FROM canal-questoes/engine:dev

USER root

COPY engine/test ./test
COPY engine/conformance ./conformance

# Área de repositórios de teste: <ENGINE_REPOS_ROOT>/<sha256_hex(repo)>/<commit>/.
# fixtures/cq-test-course e fixtures/cq-override-course no commit fixo 1111…1111.
COPY engine/test/fixtures/courses/cq-test-course /repos/511bd4373e5ce17ffaf75f5c484e80afcbaa28b65bd633c30242ffc5ed512f8d/1111111111111111111111111111111111111111
COPY engine/test/fixtures/courses/cq-override-course /repos/c92033d9d541cdab2d8b535eb6711fda5deb07263a94b0c1e4be09ca1f74b892/1111111111111111111111111111111111111111
# https://github.com/PrairieLearn/PrairieLearn no commit a193f72 → o exampleCourse embutido na imagem.
RUN mkdir -p /repos/70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7 \
 && ln -s /PrairieLearn/exampleCourse /repos/70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7/a193f72048530a4499252c454acca963cdf98922 \
 && chmod -R a+rX /repos ./test ./conformance

USER 65534:65534
CMD ["node", "--test", "--test-concurrency=1", "test/**/*.test.ts"]
