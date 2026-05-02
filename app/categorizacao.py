from .db import pool
from .utils import (
    normalizar_nome, deve_ignorar, parse_parcelas,
    hash_lancamento, hash_parcelado, hash_recorrente, voltar_meses,
)


def classificar_lancamento(conn, conta: str, nome_raw: str,
                            valor: float, data_lancamento: str,
                            linha: int) -> dict | None:
    """
    Classifica 1 lançamento.
    Retorna dict pronto pra insert em fact_lancamento, ou None se deve ignorar.

    Sequência:
      1. Ignorar lançamentos de sistema
      2. Cashback (valor < 0) → Receita
      3. Parcelado → upsert dim_lancamentos_parcelados, busca categoria
      4. Recorrente → lookup dim_lancamentos_recorrentes (nunca insere)
      5. Variável sem categoria → pendente (LLM na fase 2)
    """
    nome = normalizar_nome(nome_raw)

    if deve_ignorar(nome):
        return None

    # cashback / estorno
    if valor < 0:
        tipo = "Receita"
        valor = abs(valor)
    else:
        tipo = "Despesa"

    funcao = "Crédito"
    classe = "Variável"
    categoria = None
    subcategoria = None
    descricao = None
    id_parcelamento = None
    id_recorrencia = None
    parcela_atual = None
    parcelas_totais = None
    nome_lancamento = nome
    fonte_categoria = "pendente"

    eh_parc, p_atual, p_totais, nome_limpo = parse_parcelas(nome)

    if eh_parc:
        nome_lancamento = nome_limpo
        parcela_atual = p_atual
        parcelas_totais = p_totais
        data_inicio = voltar_meses(data_lancamento, p_atual - 1)
        id_parc = hash_parcelado(conta, nome_limpo, data_inicio, p_totais, round(valor, 2))
        id_parcelamento = id_parc

        cat = _upsert_parcelado(conn, id_parc, conta, nome_limpo,
                                data_inicio, p_totais, round(valor, 2))
        if cat["categoria"]:
            categoria = cat["categoria"]
            subcategoria = cat["subcategoria"]
            descricao = cat["descricao"]
            fonte_categoria = "parcelado"
        # sem categoria → continua como pendente, parcelamento já registrado

    else:
        # lookup recorrente (nunca insere)
        rec = _lookup_recorrente(conn, conta, nome)
        if rec:
            classe = "Fixa"
            id_recorrencia = rec["id"]
            categoria = rec["categoria"]
            subcategoria = rec["subcategoria"]
            descricao = rec["descricao"]
            fonte_categoria = "recorrente"

    hash_nat = hash_lancamento(conta, data_lancamento, nome_lancamento, valor, linha)

    return {
        "hash_natural": hash_nat,
        "data_lancamento": data_lancamento,
        "conta": conta,                 # resolve conta_id no insert via subquery
        "tipo": tipo,
        "funcao": funcao,
        "nome_original": nome_raw,
        "nome_lancamento": nome_lancamento,
        "valor": round(valor, 2),
        "linha_arquivo": linha,
        "classe": classe,
        "categoria": categoria,
        "subcategoria": subcategoria,
        "descricao": descricao,
        "fonte_categoria": fonte_categoria,
        "id_parcelamento": id_parcelamento,
        "id_recorrencia": id_recorrencia,
        "parcela_atual": parcela_atual,
        "parcelas_totais": parcelas_totais,
    }


# ─── helpers ────────────────────────────────────────────────────────────────

def _upsert_parcelado(conn, id_parc, conta, nome, data_inicio,
                      p_totais, valor_parcela) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into dim_lancamentos_parcelados
              (id, conta, nome_lancamento, data_inicio, parcelas_totais, valor_parcela)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (id) do nothing
            """,
            (id_parc, conta, nome, data_inicio, p_totais, valor_parcela),
        )
        cur.execute(
            "select categoria, subcategoria, descricao "
            "from dim_lancamentos_parcelados where id = %s",
            (id_parc,),
        )
        r = cur.fetchone()
    return {"categoria": r[0], "subcategoria": r[1], "descricao": r[2]}


def _lookup_recorrente(conn, conta: str, nome: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, categoria, subcategoria, descricao
            from dim_lancamentos_recorrentes
            where conta = %s and nome_lancamento = %s and ativo = true
            """,
            (conta, nome),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "categoria": row[1],
            "subcategoria": row[2], "descricao": row[3]}
