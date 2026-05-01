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
    linhas = []
    for _, row in df.iterrows():
        raw_data = row[cols["data"]]
        # Tenta ISO primeiro (YYYY-MM-DD), depois BR (DD/MM/YYYY), depois auto
        data = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                data = pd.to_datetime(raw_data, format=fmt).strftime("%Y-%m-%d")
                break
            except Exception:
                continue
        if data is None:
            try:
                data = pd.to_datetime(raw_data).strftime("%Y-%m-%d")
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
