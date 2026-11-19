"""Permissões de token, por escopo: tokens, pessoas, eventos, identidade, oferecimentos e bots."""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    TOKENS_MANAGE = "tokens:manage"
    PEOPLE_WRITE = "people:write"
    PEOPLE_READ = "people:read"
    EVENTS_READ = "events:read"
    IDENTITY_REVEAL = "identity:reveal"
    # Oferecimentos (consulta; cadastro, encerramento e arquivamento) e bots (registro e
    # ativação).
    OFFERINGS_READ = "offerings:read"
    OFFERINGS_WRITE = "offerings:write"
    BOTS_MANAGE = "bots:manage"
