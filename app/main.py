from fastapi import FastAPI, Request, Query
from pydantic import BaseModel

from .db import pool
from .parser import parse_fatura_csv
from .categorizacao import classificar_lancamento
from .utils import hash_recorrente

from contextlib import asynccontextmanager
from .db import pool

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
