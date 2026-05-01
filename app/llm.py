"""Sugestao de categoria via Gemini."""
import json
import os
from typing import Optional

from google import genai
from google.genai import types

_client: Optional["genai.Client"] = None


def _get_client() -> "genai.Client":
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
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
    """Chama Gemini para sugerir categoria."""
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

    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=200,
            temperature=0.0,
        ),
    )

    text = (resp.text or "").strip()

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
