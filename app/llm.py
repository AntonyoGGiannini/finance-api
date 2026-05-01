"""Sugestao de categoria via Claude quando nao ha match nas dims."""
import json
import os
from typing import Optional

from anthropic import Anthropic

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = """Voce categoriza transacoes financeiras pessoais em PT-BR.
Responda APENAS um JSON valido, sem markdown e sem explicacao.

Use estritamente as combinacoes (categoria, subcategoria) da taxonomia abaixo.
Se nao houver subcategoria adequada, deixe subcategoria como string vazia.

Schema obrigatorio:
{"categoria":"...","subcategoria":"...","classe":"Fixa|Variavel","confianca":0.0-1.0}

Regras:
- "Fixa": despesa recorrente mensal de mesmo valor (aluguel, condominio, mensalidade, internet, streaming).
- "Variavel": qualquer outra (compra de mercado, restaurante, posto, etc).
- "confianca": 0.9+ se voce tem alta certeza pela descricao; 0.5-0.8 se for inferencia razoavel; <0.5 se for chute.
"""


def sugerir_categoria(
    nome: str,
    valor: float,
    tipo: str,
    funcao: str,
    conta: str,
    taxonomia: list[tuple[str, str]],
) -> dict:
    """Chama Claude para sugerir categoria. Retorna dict com categoria/subcategoria/classe/confianca."""
    client = _get_client()

    tax_str = "\n".join(
        f"- {cat}" + (f" > {sub}" if sub else "") for cat, sub in taxonomia
    )

    user_msg = (
        f"Conta: {conta}\n"
        f"Tipo: {tipo}\n"
        f"Funcao: {funcao}\n"
        f"Nome: {nome}\n"
        f"Valor: R$ {valor:.2f}\n\n"
        f"Taxonomia permitida:\n{tax_str}"
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return {
            "categoria": "",
            "subcategoria": "",
            "classe": "Variável",
            "confianca": 0.0,
        }

    return {
        "categoria": out.get("categoria", ""),
        "subcategoria": out.get("subcategoria", "") or "",
        "classe": out.get("classe", "Variável").replace("Variavel", "Variável"),
        "confianca": float(out.get("confianca", 0.0)),
    }
