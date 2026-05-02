import hashlib
import re
import unicodedata

_RE_PARCELA = re.compile(
    r"(?:parcela\s*)?(\d{1,2})\s*[\/de ]{1,3}\s*(\d{1,2})\b",
    re.IGNORECASE,
)


def normalizar_nome(nome: str) -> str:
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = re.sub(r"\s+", " ", nome).strip().upper()
    nome = nome.replace("SAO PAULO BRA", "").strip()
    return nome


def parse_parcelas(nome: str) -> tuple[bool, int, int, str]:
    """Retorna (eh_parcelado, parcela_atual, parcelas_totais, nome_limpo)."""
    m = _RE_PARCELA.search(nome)
    if not m:
        return False, 0, 0, nome
    p, pt = int(m.group(1)), int(m.group(2))
    if pt < 2 or p > pt:
        return False, 0, 0, nome
    nome_limpo = _RE_PARCELA.sub("", nome).strip(" -()")
    nome_limpo = re.sub(r"\s+", " ", nome_limpo)
    return True, p, pt, nome_limpo


def hash_parcelado(conta: str, nome: str, data_inicio: str,
                   parcelas_totais: int, valor_parcela: float) -> str:
    raw = f"{conta}|{nome}|{data_inicio}|{parcelas_totais}|{valor_parcela:.2f}"
    return hashlib.md5(raw.encode()).hexdigest()


def hash_recorrente(conta: str, nome: str) -> str:
    raw = f"{conta}|{nome}"
    return hashlib.md5(raw.encode()).hexdigest()
