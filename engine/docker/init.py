# PID 1 do container do motor: repassa os sinais ao processo principal e recolhe os
# processos órfãos. Sem ele, o node é o PID 1 e só espera pelos filhos que ele mesmo criou; o worker
# que o zygote.py do PL mata sem esperar (terminate_worker, num tempo esgotado ou numa parada
# forçada) fica zumbi até o container acabar, e um container com limite de PIDs deixa de criar
# executores. A imagem do PL não traz tini nem outro init; este faz, com a biblioteca padrão do
# Python do PL, o que o tini faz no uso do motor:
#   - bloqueia os sinais que espera: bloqueado, o sinal chega ao PID 1 mesmo sem tratador, e o
#     sigwaitinfo o recebe;
#   - cria o filho por fork + exec, com as disposições padrão e a máscara vazia (o posix_spawn da
#     glibc deixaria ignorados no filho os sinais internos 32 e 33);
#   - repassa ao filho SIGTERM (docker stop), SIGINT, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2 e SIGWINCH;
#   - a cada SIGCHLD, recolhe todos os filhos que terminaram (waitpid(-1)), os órfãos incluídos;
#   - sai quando o filho principal sai, com o código dele (128 + n se ele morreu pelo sinal n).
# O filho recebe o ambiente original do processo (/proc/self/environ), e não o os.environ: a coerção
# de locale do Python (PEP 538) acrescenta LC_CTYPE=C.UTF-8 ao os.environ num container sem locale,
# e o motor e o zygote do PL não devem herdar essa diferença.
# Uso: python3.13 -I -S init.py -- comando [argumentos...]
import os
import signal
import sys

FORWARDED = (
    signal.SIGTERM,
    signal.SIGINT,
    signal.SIGHUP,
    signal.SIGQUIT,
    signal.SIGUSR1,
    signal.SIGUSR2,
    signal.SIGWINCH,
)
# O Python ignora SIGPIPE e SIGXFSZ ao iniciar, e o exec manteria o filho ignorando-os (como o
# restore_signals do subprocess); o SIGINT tem tratador do Python até o exec.
RESTORED = (signal.SIGPIPE, signal.SIGXFSZ)


def original_environ():
    """O ambiente com que o processo foi executado, antes de qualquer mudança do próprio Python."""
    with open('/proc/self/environ', 'rb') as f:
        entries = f.read().split(b'\0')
    env = {}
    for entry in entries:
        key, sep, value = entry.partition(b'=')
        if sep and key:
            env[key] = value
    return env


def exec_child(argv, env):
    """No filho: disposições padrão, máscara vazia e exec; sem exec, sai como o shell (127 ou 126)."""
    try:
        for signo in FORWARDED + RESTORED:
            signal.signal(signo, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, ())
        os.execvpe(argv[0], argv, env)
    except OSError as err:
        os.write(2, f'init.py: não foi possível executar {argv[0]}: {err.strerror}\n'.encode())
        os._exit(127 if isinstance(err, FileNotFoundError) else 126)
    finally:
        os._exit(126)


def exit_code(status):
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def main(argv):
    if argv[:1] == ['--']:
        argv = argv[1:]
    if not argv:
        sys.stderr.write('uso: init.py -- comando [argumentos...]\n')
        return 2
    env = original_environ()
    # Com o SIGCHLD herdado como ignorado (SIG_IGN), o kernel recolheria os filhos sozinho, sem
    # status: o waitpid daria ECHILD e o init não sairia nunca. A disposição padrão mantém o status.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    waited = set(FORWARDED) | {signal.SIGCHLD}
    signal.pthread_sigmask(signal.SIG_BLOCK, waited)
    child = os.fork()
    if child == 0:
        exec_child(argv, env)
    while True:
        signo = signal.sigwaitinfo(waited).si_signo
        if signo != signal.SIGCHLD:
            try:
                os.kill(child, signo)
            except ProcessLookupError:
                pass
            continue
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            if pid == child:
                return exit_code(status)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
