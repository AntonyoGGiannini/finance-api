"""Categorizacao de extrato bancario (Itau).

Replica a logica de adicionar_extrato() do Streamlit em SQL puro.
"""
import hashlib
import re

from .db import pool
from .llm import sugerir_categoria


def _limpar_nome_extrato(nome: str) -> tuple[str, int]:
    """Aplica regras de limpeza especificas do extrato Itau. Retorna (nome_limpo, eh_pix)."""
    nome = re.sub(r"\s+", " ", nome).strip()

    if nome[:10] == "PIX TRANSF":
        return "PIX " + nome[11:-5].strip(), 1
    if nome[:8] == "PIX QRS ":
        return "PIX " + nome[8:-5].strip(), 1
    if nome[:3] == "PAY":
        return "PAY " + nome[3:-5].strip().replace("-", ""), 0
    if nome[:3] == "INT" and "0039" in nome:
        return "INT PM SAO PAU", 1
    if nome[:18] == "FINANC IMOBILIARIO":
        return "FINANC IMOBILIARIO", 1
    if nome[:8] == "FLEXPREV":
        return "FLEXPREV", 1
    if nome[:11] in ("INT PAG TIT", "PAG TIT INT"):
        return "PAG TIT INT", 1

    return nome, 0


def _gerar_id_lancamento(
    conta: str, funcao: str, tipo: str, data: str, nome: str, valor: float, linha: int
) -> str:
    raw = f"{conta}|{funcao}|{tipo}|{data}|{nome}|{valor}|{linha}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _carregar_taxonomia(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "select distinct categoria, coalesce(subcategoria, '') "
            "from dim_categoria order by 1, 2"
        )
        return cur.fetchall()

def processar_extrato(linhas: list[dict], conta: str) -> dict:
    inseridos = 0
    ignorados = 0
    pendentes = 0
    detalhes = []
    funcao = "Débito"

    registros = []

    with pool.connection() as conn:
        taxonomia = _carregar_taxonomia(conn)

        # FASE 1: parse + lookup determinístico
        for r, item in enumerate(linhas, start=1):
            try:
                data_lancamento = item["data"]
                nome_raw = str(item["lancamento"])
                valor = float(item["valor"])
            except (KeyError, ValueError, TypeError):
                ignorados += 1
                continue

            nome_limpo, eh_pix = _limpar_nome_extrato(nome_raw)
            tipo = "Receita" if valor > 0 else "Despesa"
            valor_lancamento = abs(valor)
            data_vencimento = data_lancamento

            categoria = ""
            subcategoria = ""
            descricao = ""
            classe = "Variável"
            id_recorrencia = None
            contexto = None

            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id, categoria, coalesce(subcategoria, ''),
                           coalesce(descricao, '')
                    from dim_lancamentos_recorrentes
                    where conta = %s
                      and tipo_lancamento = 'Débito'
                      and nome_lancamento = %s
                      and valor between %s - 0.05 and %s + 0.05
                    limit 1
                    """,
                    (conta, nome_limpo, valor_lancamento, valor_lancamento),
                )
                row = cur.fetchone()

            if row:
                classe = "Fixa"
                id_recorrencia, categoria, subcategoria, descricao = row
                contexto = 5
            elif eh_pix:
                contexto = 3
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select categoria, coalesce(subcategoria, ''),
                               coalesce(descricao, '')
                        from dim_categoria_lancamento
                        where conta = %s
                          and funcao = 'Débito'
                          and nome_lancamento = %s
                        limit 1
                        """,
                        (conta, nome_limpo),
                    )
                    row = cur.fetchone()
                if row:
                    categoria, subcategoria, descricao = row
                    contexto = 4
                else:
                    contexto = 3

            registros.append({
                "linha": r,
                "data_lancamento": data_lancamento,
                "data_vencimento": data_vencimento,
                "nome_limpo": nome_limpo,
                "valor_lancamento": valor_lancamento,
                "tipo": tipo,
                "categoria": categoria,
                "subcategoria": subcategoria,
                "descricao": descricao,
                "classe": classe,
                "id_recorrencia": id_recorrencia,
                "contexto": contexto,
                "origem_categoria": "historico",
                "revisado": True,
            })

        # FASE 2: batch LLM, separado por tipo (Receita vs Despesa)
        from .llm import categorizar_batch
        mapa_llm = {}  # chave: (tipo, nome_limpo)

        for tipo_grupo in ("Receita", "Despesa"):
            orfaos_tipo = [
                reg for reg in registros
                if not reg["categoria"] and reg["tipo"] == tipo_grupo
            ]
            nomes_unicos = list({reg["nome_limpo"] for reg in orfaos_tipo})
            if not nomes_unicos:
                continue

            try:
                for i in range(0, len(nomes_unicos), 50):
                    chunk = nomes_unicos[i:i+50]
                    sugestoes = categorizar_batch(
                        nomes=chunk,
                        taxonomia=taxonomia,
                        contexto={"tipo": tipo_grupo, "funcao": funcao, "conta": conta},
                    )
                    for s in sugestoes:
                        mapa_llm[(tipo_grupo, s["nome"])] = s
            except Exception as exc:
                import traceback
                print(f"LLM BATCH ERROR ({tipo_grupo}): {type(exc).__name__}: {exc}")
                traceback.print_exc()

        for reg in registros:
            if reg["categoria"]:
                continue
            sug = mapa_llm.get((reg["tipo"], reg["nome_limpo"]))
            if sug:
                reg["categoria"] = sug.get("categoria", "Outros")
                reg["subcategoria"] = sug.get("subcategoria", "")
                reg["classe"] = sug.get("classe", "Variável")
                reg["origem_categoria"] = "llm"
                reg["revisado"] = False
                pendentes += 1
            else:
                reg["origem_categoria"] = "llm_erro"
                reg["revisado"] = False

        # FASE 3: insert
        for reg in registros:
            id_lancamento = _gerar_id_lancamento(
                conta, funcao, reg["tipo"], reg["data_lancamento"],
                reg["nome_limpo"], reg["valor_lancamento"], reg["linha"],
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fact_lancamento (
                        id_lancamento, data_lancamento, data_vencimento, conta,
                        tipo, funcao, classe, parcelado, p, pt,
                        nome_lancamento, categoria, subcategoria, descricao,
                        valor_lancamento, id_parcelamento, id_recorrencia,
                        reembolso, contexto, origem_categoria, revisado
                    ) values (
                        %s, %s, %s, %s, %s, %s, %s, 'N', 1, 1,
                        %s, %s, %s, %s, %s, null, %s, '0', %s, %s, %s
                    )
                    on conflict (id_lancamento) do nothing
                    returning id_lancamento
                    """,
                    (
                        id_lancamento, reg["data_lancamento"], reg["data_vencimento"], conta,
                        reg["tipo"], funcao, reg["classe"],
                        reg["nome_limpo"], reg["categoria"], reg["subcategoria"],
                        reg["descricao"], reg["valor_lancamento"],
                        reg["id_recorrencia"],
                        reg["contexto"], reg["origem_categoria"], reg["revisado"],
                    ),
                )
                row = cur.fetchone()
                if row:
                    inseridos += 1
                    detalhes.append({
                        "id_lancamento": id_lancamento,
                        "nome": reg["nome_limpo"],
                        "valor": reg["valor_lancamento"],
                        "tipo": reg["tipo"],
                        "categoria": reg["categoria"],
                        "subcategoria": reg["subcategoria"],
                        "classe": reg["classe"],
                        "contexto": reg["contexto"],
                        "origem": reg["origem_categoria"],
                        "revisado": reg["revisado"],
                    })
                else:
                    ignorados += 1

        conn.commit()

    return {
        "inseridos": inseridos,
        "ignorados": ignorados,
        "sugestoes_pendentes": pendentes,
        "lancamentos": detalhes,
    }
