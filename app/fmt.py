"""Utilitarios de formatacao."""
import re

PARCELAS_RE = re.compile(r"(0[1-9]|[1-9]\d)/(0[1-9]|[1-9]\d)$")


def parse_parcelas(texto: str):
    """Detecta sufixo 'NN/MM' que indica parcelamento.

    Retorna: (parcelado, n_parcela, parcelas_totais, nome_limpo)
    """
    m = PARCELAS_RE.search(texto)
    if not m:
        return "N", 1, 1, texto

    nome_fatura = texto[:-5].strip()

    if nome_fatura == "Anuidade Diferenci":
        return "N", 1, 1, nome_fatura

    return "S", int(m.group(1)), int(m.group(2)), nome_fatura
