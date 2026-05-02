import csv
import io
from datetime import datetime


def parse_fatura_csv(raw: bytes) -> list[dict]:
    """
    Lê CSV de fatura ITAU/LATAM.
    Colunas esperadas: data, lançamento, valor
    Retorna lista de dicts com data (ISO str), lancamento (str), valor (float), linha (int).
    """
    # remove BOM se presente
    texto = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.DictReader(io.StringIO(texto))

    linhas = []
    for i, row in enumerate(reader, start=1):
        try:
            data_raw = (row.get("data") or row.get("Data") or "").strip()
            nome_raw = (row.get("lançamento") or row.get("lancamento")
                        or row.get("Lançamento") or "").strip()
            valor_raw = (row.get("valor") or row.get("Valor") or "0").strip()

            # normaliza data — aceita ISO e DD/MM/YYYY
            if "/" in data_raw:
                data_iso = datetime.strptime(data_raw, "%d/%m/%Y").strftime("%Y-%m-%d")
            else:
                data_iso = data_raw  # já é ISO

            valor = float(valor_raw.replace(",", "."))

            linhas.append({
                "data": data_iso,
                "lancamento": nome_raw,
                "valor": valor,
                "linha": i,
            })
        except (ValueError, TypeError):
            continue

    return linhas
