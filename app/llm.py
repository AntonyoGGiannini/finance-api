"""LLM categorizador via Gemini. Single-call e batch."""
import json
import os

from google import genai
from google.genai import types

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def _formatar_taxonomia(taxonomia: list[tuple[str, str]]) -> str:
    """taxonomia = [(categoria, subcategoria), ...] vinda de dim_categoria."""
    linhas = []
    for cat, sub in taxonomia:
        linhas.append(f"- {cat}" + (f" > {sub}" if sub else ""))
    return "\n".join(linhas)


def sugerir_categoria(
    nome: str,
    valor: float,
    tipo: str,
    funcao: str,
    conta: str,
    taxonomia: list[tuple[str, str]],
) -> dict:
    """Single-call. Mantido pra compatibilidade. Usa batch internamente com 1 item."""
    res = categorizar_batch(
        nomes=[nome],
        taxonomia=taxonomia,
        contexto={"tipo": tipo, "funcao": funcao, "conta": conta},
    )
    if not res:
        raise RuntimeError("LLM retornou vazio")
    r = res[0]
    return {
        "categoria": r["categoria"],
        "subcategoria": r.get("subcategoria", ""),
        "classe": r.get("classe", "Variável"),
    }


def categorizar_batch(
    nomes: list[str],
    taxonomia: list[tuple[str, str]],
    contexto: dict | None = None,
) -> list[dict]:
    """Recebe lista de nomes únicos, retorna lista de dicts:
    [{nome, categoria, subcategoria, classe}]
    """
    if not nomes:
        return []

    ctx = contexto or {}
    cabecalho_ctx = ""
    if ctx:
        cabecalho_ctx = (
            f"Contexto: conta={ctx.get('conta', '?')}, "
            f"tipo={ctx.get('tipo', '?')}, funcao={ctx.get('funcao', '?')}.\n"
        )

    prompt = f"""Você é um categorizador de lançamentos financeiros pessoais (Brasil).

{cabecalho_ctx}Taxonomia válida (use APENAS estas combinações categoria > subcategoria):
{_formatar_taxonomia(taxonomia)}

Para cada nome de lançamento abaixo, retorne categoria, subcategoria e classe.
- classe = "Fixa" se for assinatura/recorrente típica (streaming, plano, mensalidade); senão "Variável".
- Se não souber subcategoria, use "".
- Se não souber categoria, use "Outros".

Retorne APENAS JSON array, sem markdown, no formato exato:
[{{"nome": "...", "categoria": "...", "subcategoria": "...", "classe": "..."}}]

Lançamentos:
{json.dumps(nomes, ensure_ascii=False)}
"""

    resp = _get_client().models.generate_content(
        model="gemini-2.5-flash",
