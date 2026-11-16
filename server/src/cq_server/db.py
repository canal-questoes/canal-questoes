"""Conexões com o PostgreSQL.

Toda conexão da aplicação passa por `connect`, que fixa o `connect_timeout` em 3 s: no Windows, o
psycopg contra uma porta fechada só falha ao fim do `connect_timeout` e, sem ele, fica preso por
mais de 60 s. Assim, a saúde responde 503 rápido e o worker volta a tentar sem travar.
"""

from __future__ import annotations

from typing import Any

import psycopg

CONNECT_TIMEOUT_SECONDS = 3


def connect(conninfo: str, *, autocommit: bool = False, **kwargs: Any) -> psycopg.Connection:
    """Abre uma conexão nova com `connect_timeout` de 3 s.

    - `conninfo`: URL (`postgresql://…`) ou `chave=valor`, como aceita o libpq.
    - `autocommit`: repassado ao psycopg (padrão `False`: a primeira instrução abre uma transação,
      que o chamador confirma com `conn.commit()` ou com `with conn.transaction():`).
    - `kwargs`: outros parâmetros do `psycopg.connect` (ex.: `options`). Um `connect_timeout`
      explícito aqui substitui o padrão.

    O chamador fecha a conexão (`with connect(...) as conn:` fecha ao sair, confirmando ou
    desfazendo a transação pendente conforme houve exceção).
    """
    kwargs.setdefault("connect_timeout", CONNECT_TIMEOUT_SECONDS)
    return psycopg.connect(conninfo, autocommit=autocommit, **kwargs)
