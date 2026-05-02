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
        contexto={"tipo": tipo, "funcao": funcao, "conta": conta, "valor": valor},
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
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    texto = (resp.text or "").strip()
    if not texto:
        raise RuntimeError("Gemini retornou resposta vazia")

    try:
        data = json.loads(texto)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Resposta do Gemini não veio em JSON válido: {texto}") from e

    if not isinstance(data, list):
        raise RuntimeError(
            f"Resposta inválida: esperado JSON array, recebido {type(data).__name__}"
        )

    saida = []
    for i, nome_original in enumerate(nomes):
        item = data[i] if i < len(data) and isinstance(data[i], dict) else {}
        saida.append(
            {
                "nome": item.get("nome") or nome_original,
                "categoria": (item.get("categoria") or "Outros").strip(),
                "subcategoria": (item.get("subcategoria") or "").strip(),
                "classe": (
                    item.get("classe")
                    if item.get("classe") in {"Fixa", "Variável"}
                    else "Variável"
                ),
            }
        )

    return saida
