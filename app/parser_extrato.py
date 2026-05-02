"""Parser de extrato ITAU em XLS (formato CDFV2 — engine xlrd)."""
import io
from datetime import datetime
import pandas as pd

_IGNORAR_NOME = {
    "SALDO ANTERIOR",
    "SALDO TOTAL DISPONIVEL DIA",
    "SALDO TOTAL DISPONÍVEL DIA",
    "S A L D O",
}


def parse_extrato_itau(raw: bytes) -> list[dict]:
    """
    Lê extrato ITAU (XLS antigo, header na linha 7).
    Retorna lista de dicts com data (ISO), lancamento, valor (float), linha.
    Ignora linhas de saldo e sem valor.
    """
    df = pd.read_excel(io.BytesIO(raw), engine="xlrd", header=7)
    df.columns = ["data", "lancamento", "ag_origem", "valor", "saldo"]

    linhas = []
    contador = 0
    for _, row in df.iterrows():
        contador += 1
        data_raw = row["data"]
        nome_raw = row["lancamento"]
        valor_raw = row["valor"]

        # pula linhas vazias, header repetido, ou sem valor (saldos)
        if pd.isna(data_raw) or pd.isna(nome_raw) or pd.isna(valor_raw):
            continue
        if not isinstance(data_raw, str) or "/" not in data_raw:
            continue

        nome = str(nome_raw).strip().upper()
        # remove acentos comuns que ITAU exporta corrompido
        nome = nome.replace("DISPONÃVEL", "DISPONIVEL").replace("DISPONÍVEL", "DISPONIVEL")

        if nome in _IGNORAR_NOME:
            continue

        try:
            data_iso = datetime.strptime(data_raw, "%d/%m/%Y").strftime("%Y-%m-%d")
            valor = float(valor_raw)
        except (ValueError, TypeError):
            continue

        linhas.append({
            "data": data_iso,
            "lancamento": str(nome_raw).strip(),
            "valor": valor,
            "linha": contador,
        })

    return linhas
