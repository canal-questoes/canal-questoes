# Fixture de ir.test.ts (opções e mídias da IR):
# o mesmo nome "x.png" em clientFilesQuestion (desta questão) e em clientFilesCourse (raiz do curso de
# teste, cq-test-course/clientFilesCourse/x.png), com bytes diferentes. As duas mídias não podem se
# confundir: filenames clientFilesQuestion/x.png e clientFilesCourse/x.png, e o content_hash de cada
# opção usa a identidade da própria mídia. O <img> com options.client_files_course_url reusa a mídia
# do pl-figure com directory="clientFilesCourse". Sem generate: o pl-multiple-choice grava o gabarito.
