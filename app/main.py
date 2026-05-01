"""API FastAPI: endpoints de processamento de fatura e extrato."""
import io
from contextlib import asynccontextmanager
from datetime import date

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from .categorizacao_extrato import processar_extrato
from .categorizacao_fatura import processar_fatura
from .db import init_pool, close_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


app = FastAPI(title="Finance API", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


def _ler_arquivo_fatura(arquivo: UploadFile) -> list[dict]:
    """Le CSV ou XLSX no formato fatura: colunas 'data', 'lançamento', 'valor'."""
    content = arquivo.file.read()
    if arquivo.filename.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(content))
    elif arquivo.filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(content))
    else:
        raise HTTPException(400, "Formato nao suportado (use .csv ou .xlsx)")

    cols = {c.lower().strip(): c for c in df.columns}
    if "data" not in cols or "valor" not in cols:
        raise HTTPException(400, f"CSV deve ter colunas 'data' e 'valor'. Achei: {list(df.columns)}")

    nome_col = cols.get("lançamento") or cols.get("lancamento") or cols.get("descricao")
    if not nome_col:
        raise HTTPException(400, "CSV deve ter coluna 'lançamento' ou 'lancamento'")

    linhas = []
    for _, row in df.iterrows():
        try:
            data = pd.to_datetime(row[cols["data"]], format="%d/%m/%Y").strftime("%Y-%m-%d")
        except Exception:
            try:
                data = pd.to_datetime(row[cols["data"]]).strftime("%Y-%m-%d")
            except Exception:
                continue
        linhas.append({
            "data": data,
            "lancamento": row[nome_col],
            "valor": row[cols["valor"]],
        })
    return linhas


def _ler_arquivo_extrato(arquivo: UploadFile) -> list[dict]:
    """Le CSV do extrato Itau: header ausente, separador ';', UTF-8 BOM."""
    content = arquivo.file.read()
    if not arquivo.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Extrato deve ser CSV")

    df = pd.read_csv(io.BytesIO(content), header=None, encoding="utf-8-sig")
    df = df[0].str.strip('"').str.split(";", expand=True)
    df.columns = ["data", "lancamento", "valor"]
    df["valor"] = (
        df["valor"]
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
        .astype(float)
    )

    linhas = []
    for _, row in df.iterrows():
        try:
            data = pd.to_datetime(row["data"], format="%d/%m/%Y").strftime("%Y-%m-%d")
        except Exception:
            continue
        linhas.append({"data": data, "lancamento": row["lancamento"], "valor": row["valor"]})
    return linhas


@app.post("/processar-fatura")
def processar_fatura_endpoint(
    conta: str = Query(...),
    data_vencimento: date = Query(...),
    arquivo: UploadFile = File(...),
):
    linhas = _ler_arquivo_fatura(arquivo)
    if not linhas:
        return {"inseridos": 0, "ignorados": 0, "sugestoes_pendentes": 0, "lancamentos": []}

    return processar_fatura(
        linhas=linhas,
        conta=conta,
        data_vencimento=data_vencimento.isoformat(),
    )


@app.post("/processar-extrato")
def processar_extrato_endpoint(
    conta: str = Query(...),
    arquivo: UploadFile = File(...),
):
    linhas = _ler_arquivo_extrato(arquivo)
    if not linhas:
        return {"inseridos": 0, "ignorados": 0, "sugestoes_pendentes": 0, "lancamentos": []}

    return processar_extrato(linhas=linhas, conta=conta)
