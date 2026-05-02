import hashlib
import re
import unicodedata

_RE_PARCELA = re.compile(
    r"(\d{1,2})\s*/\s*(\d{1,2})\s*$"
)

_IGNORAR = {"PAGAMENTO EFETUADO", "CONTROLE DE SALDO"}


def normalizar_nome(nome: str) -> str:
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = re.sub(r"\s+", " ", nome).strip().upper()
    # remove sufixos de cidade comuns nas faturas ITAU
    for sufixo in ("SAO PAULO BRA", "Sao Paulo BRA", "CAMPINAS BRA"):
        nome = nome.replace(sufixo.upper(), "").strip()
    return nome


def deve_ignorar(nome: str) -> bool:
    return nome in _IGNORAR


def parse_parcelas(nome: str) -> tuple[bool, int, int, str]:
    """Retorna (eh_parcelado, parcela_atual, parcelas_totais, nome_limpo)."""
    m = _RE_PARCELA.search(nome)
    if not m:
        return False, 0, 0, nome
    p, pt = int(m.group(1)), int(m.group(2))
    if pt < 2 or p > pt:
        return False, 0, 0, nome
    nome_limpo = nome[: m.start()].strip(" -()")
    nome_limpo = re.sub(r"\s+", " ", nome_limpo)
    return True, p, pt, nome_limpo


def hash_lancamento(conta: str, data: str, nome: str,
                    valor: float, linha: int) -> str:
    """Hash de idempotência do fact_lancamento."""
    raw = f"{conta}|{data}|{nome}|{valor:.2f}|{linha}"
    return hashlib.md5(raw.encode()).hexdigest()


def hash_parcelado(conta: str, nome: str, data_inicio: str,
                   parcelas_totais: int, valor_parcela: float) -> str:
    raw = f"{conta}|{nome}|{data_inicio}|{parcelas_totais}|{valor_parcela:.2f}"
    return hashlib.md5(raw.encode()).hexdigest()


def hash_recorrente(conta: str, nome: str) -> str:
    raw = f"{conta}|{nome}"
    return hashlib.md5(raw.encode()).hexdigest()


def voltar_meses(data_iso: str, n: int) -> str:
    """Retorna o 1º dia do mês n meses antes de data_iso."""
    y, m, _ = map(int, data_iso.split("-"))
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-01"
