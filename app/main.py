from fastapi import FastAPI, Request, Query
from pydantic import BaseModel
from .db import pool
from .utils import hash_recorrente
from .categorizacao import classificar_lancamento
from .leitor import parse_arquivo  # já existe no teu projeto

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/upload-fatura")
async def upload_fatura(
    request: Request,
    conta: str = Query(...),
    data_vencimento: str = Query(...),
):
    raw = await request.body()
    linhas = parse_arquivo(raw)  # lista de dicts {data, lancamento, valor}

    resultado = {"inseridos": 0, "ignorados": 0, "pendentes_revisao": 0}

    with pool.connection() as conn:
        for item in linhas:
            try:
                cls = classificar_lancamento(
                    conn,
                    conta=conta,
                    nome_raw=str(item["lancamento"]),
                    valor=float(item["valor"]),
                    data_lancamento=item["data"],
                )
            except Exception:
                resultado["ignorados"] += 1
                continue

            if cls["categoria"] is None:
                resultado["pendentes_revisao"] += 1

            # TODO: insert em fact_lancamento usando cls + dados do item
            resultado["inseridos"] += 1

    return resultado


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
              categoria = excluded.categoria,
              subcategoria = excluded.subcategoria,
              descricao = excluded.descricao,
              valor_referencia = excluded.valor_referencia,
              atualizado_em = now()
            """,
            (rid, p.conta, p.nome_lancamento, p.valor_referencia,
             p.categoria, p.subcategoria, p.descricao),
        )
    return {"ok": True, "id": rid}


@app.get("/pendentes-revisao")
def pendentes_revisao(limit: int = 50):
    """Lista lançamentos sem categoria — alimenta a interface de marcar recorrente."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select distinct nome_lancamento, conta, count(*) as ocorrencias
            from fact_lancamento
            where categoria is null and id_parcelamento is null
            group by 1, 2
            order by 3 desc
            limit %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [{"nome": r[0], "conta": r[1], "ocorrencias": r[2]} for r in rows]
