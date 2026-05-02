from fastapi import FastAPI, Request, Query
from pydantic import BaseModel

import json
from datetime import timedelta

from .db import pool
from .parser import parse_fatura_csv
from .categorizacao import classificar_lancamento
from .utils import hash_recorrente

from contextlib import asynccontextmanager
from .db import pool

from .parser_extrato import parse_extrato_itau
from .categorizacao_extrato import classificar_extrato

@asynccontextmanager
async def lifespan(app):
    pool.open()
    yield
    pool.close()

app = FastAPI(lifespan=lifespan)

# ─── health ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True}


# ─── upload fatura ───────────────────────────────────────────────────────────

@app.post("/upload-fatura")
async def upload_fatura(
    request: Request,
    conta: str = Query(...),
    data_vencimento: str = Query(...),
):
    """
    Recebe CSV de fatura via body raw.
    Query params: conta (nome_conta em dim_account), data_vencimento (YYYY-MM-DD).
    """
    raw = await request.body()
    linhas = parse_fatura_csv(raw)

    resultado = {"inseridos": 0, "ignorados": 0, "pendentes": 0}

    with pool.connection() as conn:
        # resolve conta_id uma vez
        with conn.cursor() as cur:
            cur.execute(
                "select id_conta from dim_account where nome_conta = %s", (conta,)
            )
            row = cur.fetchone()
        if not row:
            return {"erro": f"Conta '{conta}' não encontrada em dim_account"}
        conta_id = row[0]

        for item in linhas:
            cls = classificar_lancamento(
                conn,
                conta=conta,
                nome_raw=item["lancamento"],
                valor=item["valor"],
                data_lancamento=item["data"],
                linha=item["linha"],
            )
            if cls is None:
                resultado["ignorados"] += 1
                continue

            if cls["fonte_categoria"] == "pendente":
                resultado["pendentes"] += 1

            # resolve categoria_id se tiver categoria
            categoria_id = None
            if cls["categoria"]:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id_categoria from dim_categoria
                        where categoria = %s
                          and coalesce(subcategoria, '') = coalesce(%s, '')
                        limit 1
                        """,
                        (cls["categoria"], cls["subcategoria"]),
                    )
                    r = cur.fetchone()
                    categoria_id = r[0] if r else None

            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fact_lancamento (
                      hash_natural, data_lancamento, data_vencimento, conta_id,
                      tipo, funcao, nome_original, nome_lancamento, valor, linha_arquivo,
                      classe, categoria_id, subcategoria, descricao, fonte_categoria,
                      id_parcelamento, id_recorrencia, parcela_atual, parcelas_totais
                    ) values (
                      %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s
                    )
                    on conflict (hash_natural) do nothing
                    """,
                    (
                        cls["hash_natural"], cls["data_lancamento"], data_vencimento, conta_id,
                        cls["tipo"], cls["funcao"], cls["nome_original"],
                        cls["nome_lancamento"], cls["valor"], cls["linha_arquivo"],
                        cls["classe"], categoria_id, cls["subcategoria"],
                        cls["descricao"], cls["fonte_categoria"],
                        cls["id_parcelamento"], cls["id_recorrencia"],
                        cls["parcela_atual"], cls["parcelas_totais"],
                    ),
                )
                if cur.rowcount:
                    resultado["inseridos"] += 1
                else:
                    resultado["ignorados"] += 1
                    
    # FASE 2: categoriza pendentes via Gemini em batch
    if resultado["pendentes"] > 0:
        from .llm import categorizar_batch

        with pool.connection() as conn:
            # busca taxonomia
            with conn.cursor() as cur:
                cur.execute(
                    "select categoria, coalesce(subcategoria,'') "
                    "from dim_categoria order by 1, 2"
                )
                taxonomia = cur.fetchall()

            # busca lançamentos pendentes desta fatura (pelo vencimento + conta)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select fl.id, fl.nome_lancamento
                    from fact_lancamento fl
                    join dim_account da on da.id_conta = fl.conta_id
                    where da.nome_conta = %s
                      and fl.data_vencimento = %s
                      and fl.fonte_categoria = 'pendente'
                    """,
                    (conta, data_vencimento),
                )
                pendentes = cur.fetchall()  # [(id, nome), ...]

        if pendentes:
            nomes_unicos = list({r[1] for r in pendentes})
            try:
                sugestoes = categorizar_batch(
                    nomes=nomes_unicos,
                    taxonomia=taxonomia,
                    contexto={"conta": conta, "tipo": "Despesa", "funcao": "Crédito"},
                )
                # índice por nome
                idx = {s["nome"]: s for s in sugestoes}

                with pool.connection() as conn:
                    for lid, nome in pendentes:
                        sug = idx.get(nome)
                        if not sug:
                            continue
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                update fact_lancamento set
                                  fonte_categoria = 'llm',
                                  categoria_id = (
                                    select id_categoria from dim_categoria
                                    where categoria = %s
                                      and coalesce(subcategoria,'') = coalesce(%s,'')
                                    limit 1
                                  ),
                                  subcategoria = %s,
                                  classe = %s,
                                  atualizado_em = now()
                                where id = %s
                                """,
                                (
                                    sug["categoria"],
                                    sug.get("subcategoria", ""),
                                    sug.get("subcategoria", ""),
                                    sug.get("classe", "Variável"),
                                    lid,
                                ),
                            )
                resultado["categorizados_llm"] = len(pendentes)
                resultado["pendentes"] = 0
            except Exception as e:
                resultado["erro_llm"] = str(e)
                
    return resultado


# ─── marcar recorrente ───────────────────────────────────────────────────────

class RecorrentePayload(BaseModel):
    conta: str
    nome_lancamento: str
    categoria: str
    subcategoria: str | None = None
    descricao: str | None = None
    valor_referencia: float | None = None


@app.post("/marcar-recorrente")
def marcar_recorrente(p: RecorrentePayload):
    rid = hash_recorrente(p.conta, p.nome_lancamento)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into dim_lancamentos_recorrentes
              (id, conta, nome_lancamento, valor_referencia,
               categoria, subcategoria, descricao)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (conta, nome_lancamento) do update set
              categoria        = excluded.categoria,
              subcategoria     = excluded.subcategoria,
              descricao        = excluded.descricao,
              valor_referencia = excluded.valor_referencia,
              atualizado_em    = now()
            returning id
            """,
            (rid, p.conta, p.nome_lancamento, p.valor_referencia,
             p.categoria, p.subcategoria, p.descricao),
        )
        returned_id = cur.fetchone()[0]

    # propaga categoria pros lançamentos já inseridos desse nome
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update fact_lancamento fl
                set
                  id_recorrencia  = %s,
                  classe          = 'Fixa',
                  fonte_categoria = 'recorrente',
                  categoria_id    = (
                    select id_categoria from dim_categoria
                    where categoria = %s
                      and coalesce(subcategoria,'') = coalesce(%s,'')
                    limit 1
                  ),
                  subcategoria    = %s,
                  descricao       = %s,
                  atualizado_em   = now()
                where fl.nome_lancamento = %s
                  and fl.conta_id = (
                    select id_conta from dim_account where nome_conta = %s
                  )
                  and fl.fonte_categoria in ('pendente', 'recorrente')
                """,
                (
                    returned_id,
                    p.categoria, p.subcategoria,
                    p.subcategoria, p.descricao,
                    p.nome_lancamento, p.conta,
                ),
            )

    return {"ok": True, "id": returned_id}


# ─── pendentes de revisão ────────────────────────────────────────────────────

@app.get("/pendentes")
def pendentes(limit: int = 50):
    """Lançamentos sem categoria — alimenta interface de revisão."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select
              fl.nome_lancamento,
              da.nome_conta,
              count(*) as ocorrencias,
              sum(fl.valor) as valor_total
            from fact_lancamento fl
            join dim_account da on da.id_conta = fl.conta_id
            where fl.fonte_categoria = 'pendente'
              and fl.id_parcelamento is null
            group by 1, 2
            order by 3 desc
            limit %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {"nome": r[0], "conta": r[1], "ocorrencias": r[2], "valor_total": float(r[3])}
        for r in rows
    ]

# ─── revisar lançamento ───────────────────────────────────────────────────────

class RevisaoPayload(BaseModel):
    hash_natural: str
    categoria: str
    subcategoria: str | None = None
    descricao: str | None = None

@app.post("/revisar-lancamento")
def revisar_lancamento(p: RevisaoPayload):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update fact_lancamento set
              categoria_id = (
                select id_categoria from dim_categoria
                where categoria = %s
                  and coalesce(subcategoria,'') = coalesce(%s,'')
                limit 1
              ),
              subcategoria  = %s,
              descricao     = %s,
              fonte_categoria = 'manual',
              revisado_em   = now(),
              atualizado_em = now()
            where hash_natural = %s
            returning id
            """,
            (p.categoria, p.subcategoria, p.subcategoria, p.descricao, p.hash_natural),
        )
        row = cur.fetchone()
    if not row:
        return {"erro": "lançamento não encontrado"}
    return {"ok": True, "id": row[0]}


# ─── revisar parcelado ────────────────────────────────────────────────────────

class RevisaoParceladoPayload(BaseModel):
    id_parcelamento: str
    categoria: str
    subcategoria: str | None = None
    descricao: str | None = None

@app.post("/revisar-parcelado")
def revisar_parcelado(p: RevisaoParceladoPayload):
    with pool.connection() as conn:
        # atualiza a dim (a "compra-mãe")
        with conn.cursor() as cur:
            cur.execute(
                """
                update dim_lancamentos_parcelados set
                  categoria     = %s,
                  subcategoria  = %s,
                  descricao     = %s,
                  atualizado_em = now()
                where id = %s
                """,
                (p.categoria, p.subcategoria, p.descricao, p.id_parcelamento),
            )

        # propaga pra todas as parcelas no fact
        with conn.cursor() as cur:
            cur.execute(
                """
                update fact_lancamento set
                  categoria_id = (
                    select id_categoria from dim_categoria
                    where categoria = %s
                      and coalesce(subcategoria,'') = coalesce(%s,'')
                    limit 1
                  ),
                  subcategoria    = %s,
                  descricao       = %s,
                  fonte_categoria = 'manual',
                  revisado_em     = now(),
                  atualizado_em   = now()
                where id_parcelamento = %s
                returning id
                """,
                (p.categoria, p.subcategoria, p.subcategoria, p.descricao, p.id_parcelamento),
            )
            parcelas_atualizadas = cur.rowcount

    return {"ok": True, "parcelas_atualizadas": parcelas_atualizadas}

# ─── upload extrato ──────────────────────────────────────────────────────────
# Adicionar ao main.py — imports adicionais no topo

@app.post("/upload-extrato")
async def upload_extrato(
    request: Request,
    conta: str = Query(...),
):
    """
    Recebe XLS de extrato ITAU via body raw.
    Não precisa de data_vencimento (extrato não tem).
    """
    raw = await request.body()
    linhas = parse_extrato_itau(raw)

    resultado = {"inseridos": 0, "ignorados": 0, "pendentes": 0}

    with pool.connection() as conn:
        # resolve conta_id
        with conn.cursor() as cur:
            cur.execute(
                "select id_conta from dim_account where nome_conta = %s", (conta,)
            )
            row = cur.fetchone()
        if not row:
            return {"erro": f"Conta '{conta}' não encontrada em dim_account"}
        conta_id = row[0]

        for item in linhas:
            cls = classificar_extrato(
                conn,
                conta=conta,
                nome_raw=item["lancamento"],
                valor=item["valor"],
                data_lancamento=item["data"],
                linha=item["linha"],
            )

            if cls["fonte_categoria"] == "pendente":
                resultado["pendentes"] += 1

            categoria_id = None
            if cls["categoria"]:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id_categoria from dim_categoria
                        where categoria = %s
                          and coalesce(subcategoria, '') = coalesce(%s, '')
                        limit 1
                        """,
                        (cls["categoria"], cls["subcategoria"]),
                    )
                    r = cur.fetchone()
                    categoria_id = r[0] if r else None

            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fact_lancamento (
                      hash_natural, data_lancamento, conta_id,
                      tipo, funcao, nome_original, nome_lancamento, valor, linha_arquivo,
                      classe, categoria_id, subcategoria, descricao, fonte_categoria,
                      id_recorrencia
                    ) values (
                      %s, %s, %s,
                      %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s
                    )
                    on conflict (hash_natural) do nothing
                    """,
                    (
                        cls["hash_natural"], cls["data_lancamento"], conta_id,
                        cls["tipo"], cls["funcao"], cls["nome_original"],
                        cls["nome_lancamento"], cls["valor"], cls["linha_arquivo"],
                        cls["classe"], categoria_id, cls["subcategoria"],
                        cls["descricao"], cls["fonte_categoria"],
                        cls["id_recorrencia"],
                    ),
                )
                if cur.rowcount:
                    resultado["inseridos"] += 1
                else:
                    resultado["ignorados"] += 1

    # FASE 2: LLM pros pendentes (mesma lógica do upload-fatura, sem filtro por vencimento)
    if resultado["pendentes"] > 0:
        from .llm import categorizar_batch

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select categoria, coalesce(subcategoria,'') "
                    "from dim_categoria order by 1, 2"
                )
                taxonomia = cur.fetchall()

            with conn.cursor() as cur:
                cur.execute(
                    """
                    select fl.id, fl.nome_lancamento, fl.tipo, fl.funcao
                    from fact_lancamento fl
                    join dim_account da on da.id_conta = fl.conta_id
                    where da.nome_conta = %s
                      and fl.fonte_categoria = 'pendente'
                      and fl.criado_em > now() - interval '5 minutes'
                    """,
                    (conta,),
                )
                pendentes = cur.fetchall()

        if pendentes:
            nomes_unicos = list({r[1] for r in pendentes})
            try:
                sugestoes = categorizar_batch(
                    nomes=nomes_unicos,
                    taxonomia=taxonomia,
                    contexto={"conta": conta, "tipo": "Despesa", "funcao": "Débito"},
                )
                idx = {s["nome"]: s for s in sugestoes}

                with pool.connection() as conn:
                    for lid, nome, _, _ in pendentes:
                        sug = idx.get(nome)
                        if not sug:
                            continue
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                update fact_lancamento set
                                  fonte_categoria = 'llm',
                                  categoria_id = (
                                    select id_categoria from dim_categoria
                                    where categoria = %s
                                      and coalesce(subcategoria,'') = coalesce(%s,'')
                                    limit 1
                                  ),
                                  subcategoria = %s,
                                  classe = %s,
                                  atualizado_em = now()
                                where id = %s
                                """,
                                (
                                    sug["categoria"],
                                    sug.get("subcategoria", ""),
                                    sug.get("subcategoria", ""),
                                    sug.get("classe", "Variável"),
                                    lid,
                                ),
                            )
                resultado["categorizados_llm"] = len(pendentes)
                resultado["pendentes"] = 0
            except Exception as e:
                resultado["erro_llm"] = str(e)

    return resultado

# Adicionar ao app/main.py — endpoint de dados pro squad

@app.get("/squad-data")
def squad_data():
    """
    Retorna JSON consolidado pros 4 agentes do squad mensal.
    Janela: últimos 35 dias (5 semanas) a partir da data mais recente em fact_lancamento.
    Janela comparativa: 105 dias anteriores ao período (3 períodos de 35d pra trimestre).
    """
    with pool.connection() as conn:
        # 1. Define janela
        with conn.cursor() as cur:
            cur.execute("select max(data_lancamento) from fact_lancamento")
            ref = cur.fetchone()[0]
        if not ref:
            return {"erro": "sem dados em fact_lancamento"}

        out = {
            "periodo": {
                "ref_max": str(ref),
                "inicio": str(ref - __import__('datetime').timedelta(days=34)),
                "fim": str(ref),
                "comparativo_inicio": str(ref - __import__('datetime').timedelta(days=139)),
                "comparativo_fim": str(ref - __import__('datetime').timedelta(days=35)),
            }
        }

        # 2. Totais do período (receita, despesa, saldo)
        with conn.cursor() as cur:
            cur.execute(
                """
                select tipo,
                       coalesce(sum(valor) filter (where classe != 'Investimento'), 0) as total,
                       coalesce(sum(valor) filter (where classe = 'Fixa'), 0) as fixa,
                       coalesce(sum(valor) filter (where classe = 'Variável'), 0) as variavel
                from fact_lancamento
                where data_lancamento >= %s and data_lancamento <= %s
                group by tipo
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"]),
            )
            out["totais"] = [
                {"tipo": r[0], "total": float(r[1]),
                 "fixa": float(r[2]), "variavel": float(r[3])}
                for r in cur.fetchall()
            ]

        # 3. Top categorias do período (com comparativo trimestre)
        with conn.cursor() as cur:
            cur.execute(
                """
                with periodo as (
                  select dc.categoria, dc.subcategoria,
                         sum(fl.valor) as total_periodo
                  from fact_lancamento fl
                  join dim_categoria dc on dc.id_categoria = fl.categoria_id
                  where fl.tipo = 'Despesa' and fl.classe != 'Investimento'
                    and fl.data_lancamento between %s and %s
                  group by 1, 2
                ),
                trimestre as (
                  select dc.categoria, dc.subcategoria,
                         sum(fl.valor) / 3.0 as media_periodo
                  from fact_lancamento fl
                  join dim_categoria dc on dc.id_categoria = fl.categoria_id
                  where fl.tipo = 'Despesa' and fl.classe != 'Investimento'
                    and fl.data_lancamento between %s and %s
                  group by 1, 2
                )
                select p.categoria, p.subcategoria, p.total_periodo,
                       coalesce(t.media_periodo, 0) as media_3m
                from periodo p
                left join trimestre t using (categoria, subcategoria)
                order by p.total_periodo desc
                limit 15
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"],
                 out["periodo"]["comparativo_inicio"], out["periodo"]["comparativo_fim"]),
            )
            out["categorias"] = [
                {"categoria": r[0], "subcategoria": r[1],
                 "total": float(r[2]), "media_3m": float(r[3]),
                 "variacao_pct": round((float(r[2]) - float(r[3])) / float(r[3]) * 100, 1)
                                if float(r[3]) > 0 else None}
                for r in cur.fetchall()
            ]

        # 4. Top 10 maiores gastos individuais
        with conn.cursor() as cur:
            cur.execute(
                """
                select fl.data_lancamento, fl.nome_lancamento, fl.valor,
                       dc.categoria, dc.subcategoria, da.nome_conta
                from fact_lancamento fl
                left join dim_categoria dc on dc.id_categoria = fl.categoria_id
                join dim_account da on da.id_conta = fl.conta_id
                where fl.tipo = 'Despesa' and fl.classe != 'Investimento'
                  and fl.data_lancamento between %s and %s
                order by fl.valor desc
                limit 10
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"]),
            )
            out["maiores_gastos"] = [
                {"data": str(r[0]), "nome": r[1], "valor": float(r[2]),
                 "categoria": r[3], "subcategoria": r[4], "conta": r[5]}
                for r in cur.fetchall()
            ]

        # 5. Recorrentes ativos (assinaturas)
        with conn.cursor() as cur:
            cur.execute(
                """
                select dlr.nome_lancamento, dlr.categoria, dlr.subcategoria,
                       dlr.valor_referencia,
                       count(fl.id) as ocorrencias_periodo,
                       coalesce(sum(fl.valor), 0) as total_periodo
                from dim_lancamentos_recorrentes dlr
                left join fact_lancamento fl on fl.id_recorrencia = dlr.id
                  and fl.data_lancamento between %s and %s
                where dlr.ativo = true
                group by 1, 2, 3, 4
                order by total_periodo desc
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"]),
            )
            out["recorrentes"] = [
                {"nome": r[0], "categoria": r[1], "subcategoria": r[2],
                 "valor_referencia": float(r[3]) if r[3] else None,
                 "ocorrencias": r[4], "total_periodo": float(r[5])}
                for r in cur.fetchall()
            ]

        # 6. Parcelados ativos (com horizonte)
        with conn.cursor() as cur:
            cur.execute(
                """
                select dlp.nome_lancamento, dlp.parcelas_totais, dlp.valor_parcela,
                       dlp.data_inicio, dlp.categoria,
                       count(fl.id) as parcelas_pagas,
                       (dlp.parcelas_totais - count(fl.id)) as parcelas_restantes
                from dim_lancamentos_parcelados dlp
                left join fact_lancamento fl on fl.id_parcelamento = dlp.id
                group by 1, 2, 3, 4, 5, dlp.id
                having (dlp.parcelas_totais - count(fl.id)) > 0
                order by dlp.valor_parcela * (dlp.parcelas_totais - count(fl.id)) desc
                """,
            )
            out["parcelados_ativos"] = [
                {"nome": r[0], "parcelas_totais": r[1],
                 "valor_parcela": float(r[2]), "data_inicio": str(r[3]),
                 "categoria": r[4], "parcelas_pagas": r[5],
                 "parcelas_restantes": r[6],
                 "compromisso_restante": float(r[2]) * r[6]}
                for r in cur.fetchall()
            ]

        # 7. Frequência de gastos variáveis (top categorias por nº de ocorrências)
        with conn.cursor() as cur:
            cur.execute(
                """
                select dc.categoria, dc.subcategoria,
                       count(*) as ocorrencias,
                       sum(fl.valor) as total
                from fact_lancamento fl
                join dim_categoria dc on dc.id_categoria = fl.categoria_id
                where fl.tipo = 'Despesa' and fl.classe = 'Variável'
                  and fl.data_lancamento between %s and %s
                group by 1, 2
                having count(*) >= 3
                order by ocorrencias desc
                limit 15
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"]),
            )
            out["frequencia_variaveis"] = [
                {"categoria": r[0], "subcategoria": r[1],
                 "ocorrencias": r[2], "total": float(r[3])}
                for r in cur.fetchall()
            ]

        # 8. Investimentos do período (aplicações vs resgates vs proventos)
        with conn.cursor() as cur:
            cur.execute(
                """
                select fl.subcategoria, fl.tipo,
                       sum(fl.valor) as total,
                       count(*) as ocorrencias
                from fact_lancamento fl
                join dim_categoria dc on dc.id_categoria = fl.categoria_id
                where fl.data_lancamento between %s and %s
                  and (dc.categoria = 'Investimentos'
                       or dc.subcategoria in ('Juros Recebidos', 'Dividendos'))
                group by 1, 2
                order by total desc
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"]),
            )
            out["investimentos"] = [
                {"subcategoria": r[0], "tipo": r[1],
                 "total": float(r[2]), "ocorrencias": r[3]}
                for r in cur.fetchall()
            ]

        # 9. Saldo apertado: dias com saldo bancário baixo
        # (Aproximação: dias com saldo abaixo de 10% da maior receita do período)
        with conn.cursor() as cur:
            cur.execute(
                """
                select max(valor) as maior_receita
                from fact_lancamento
                where tipo = 'Receita' and classe != 'Investimento'
                  and data_lancamento between %s and %s
                """,
                (out["periodo"]["inicio"], out["periodo"]["fim"]),
            )
            r = cur.fetchone()
            out["maior_receita_periodo"] = float(r[0]) if r and r[0] else 0

        # 10. Posições de investimento (vazia se tabela ainda não populada)
        with conn.cursor() as cur:
            cur.execute(
                """
                select classe_ativo, count(*) as ativos,
                       sum(mtm) as patrimonio
                from posicoes
                where data_ref = (select max(data_ref) from posicoes)
                group by 1
                """
            )
            out["posicoes_atuais"] = [
                {"classe": r[0], "ativos": r[1], "patrimonio": float(r[2]) if r[2] else 0}
                for r in cur.fetchall()
            ]

    return out

# Adicionar ao app/main.py — persiste output dos agentes em `analises`

class AnalisePayload(BaseModel):
    agente: str  # 'diagnostico' | 'habitos' | 'carteira' | 'protecao' | 'consolidado'
    mes_ref: str  # YYYY-MM-DD (1º dia do mês analisado)
    resumo: str
    payload: dict | None = None


@app.post("/salvar-analise")
def salvar_analise(p: AnalisePayload):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into analises (agente, mes_ref, resumo, payload)
            values (%s, %s, %s, %s)
            on conflict (agente, mes_ref) do update set
              resumo = excluded.resumo,
              payload = excluded.payload,
              criado_em = now()
            returning id
            """,
            (p.agente, p.mes_ref, p.resumo,
             __import__('json').dumps(p.payload) if p.payload else None),
        )
        rid = cur.fetchone()[0]
    return {"ok": True, "id": rid}
