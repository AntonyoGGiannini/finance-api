"""Classificação de lançamentos de extrato (conta corrente).

Diferenças do fatura:
- Sem parcelado
- Função detectada pelo prefixo do nome (PIX, TED, etc.)
- Tipo definido pelo sinal do valor
- Regras determinísticas pra padrões ITAU (resgate, aplicação, salário, rendimento)
"""
import re

from .utils import normalizar_nome, hash_lancamento, hash_recorrente


# Regras determinísticas — evita LLM pra padrões previsíveis do banco
_REGRAS_NOME = [
    # nome_pattern (regex), categoria, subcategoria, classe
    (r"^REND PAGO", "Renda", "Juros Recebidos", "Variável"),
    (r"^REMUNERACAO|^SALARIO|^SALÁRIO", "Renda", "Salário", "Fixa"),
    (r"^DEP DIN", "Renda", "Depósito", "Variável"),
    (r"^RESGATE CDB|^RESGATE LCI|^RESGATE LCA|^RESGATE FUNDO", "Investimentos", "Resgate", "Investimento"),
    (r"^APLICACAO|^APLICAÇÃO", "Investimentos", "Aplicação", "Investimento"),
    (r"^DIVIDENDOS|^JCP|^JUROS S/CAPITAL", "Renda", "Dividendos", "Variável"),
    (r"^IOF\b", "Tarifas Financeiras", "Outras Tarifas Financeiras", "Variável"),
    (r"^ANUIDADE|^TARIFA|^PACOTE", "Tarifas Financeiras", "Anuidade e Pacote de Serviços", "Fixa"),
    (r"^IR\b|^IMPOSTO RENDA", "Impostos", "Imposto de Renda", "Variável"),
]


def _detectar_funcao(nome: str) -> str:
    n = nome.upper()
    if n.startswith("PIX"):
        return "Pix"
    if n.startswith(("TED", "DOC", "TRANSF")):
        return "Transferência"
    return "Débito"


def _aplicar_regra(nome: str) -> dict | None:
    for pattern, cat, sub, classe in _REGRAS_NOME:
        if re.search(pattern, nome, re.IGNORECASE):
            return {"categoria": cat, "subcategoria": sub, "classe": classe}
    return None


def classificar_extrato(conn, conta: str, nome_raw: str,
                         valor: float, data_lancamento: str,
                         linha: int) -> dict:
    """Classifica 1 lançamento de extrato. Retorna dict pronto pra insert."""
    nome = normalizar_nome(nome_raw)

    # tipo pelo sinal
    if valor < 0:
        tipo = "Despesa"
        valor_abs = abs(valor)
    else:
        tipo = "Receita"
        valor_abs = valor

    funcao = _detectar_funcao(nome)
    classe = "Variável"
    categoria = None
    subcategoria = None
    descricao = None
    id_recorrencia = None
    fonte_categoria = "pendente"

    # 1. tenta regras determinísticas (banco-specific)
    regra = _aplicar_regra(nome)
    if regra:
        categoria = regra["categoria"]
        subcategoria = regra["subcategoria"]
        classe = regra["classe"]
        fonte_categoria = "historico"

    # 2. lookup recorrente (sobrescreve se tiver)
    if not categoria:
        rec = _lookup_recorrente(conn, conta, nome)
        if rec:
            classe = "Fixa"
            id_recorrencia = rec["id"]
            categoria = rec["categoria"]
            subcategoria = rec["subcategoria"]
            descricao = rec["descricao"]
            fonte_categoria = "recorrente"

    hash_nat = hash_lancamento(conta, data_lancamento, nome, valor_abs, linha)

    return {
        "hash_natural": hash_nat,
        "data_lancamento": data_lancamento,
        "conta": conta,
        "tipo": tipo,
        "funcao": funcao,
        "nome_original": nome_raw,
        "nome_lancamento": nome,
        "valor": round(valor_abs, 2),
        "linha_arquivo": linha,
        "classe": classe,
        "categoria": categoria,
        "subcategoria": subcategoria,
        "descricao": descricao,
        "fonte_categoria": fonte_categoria,
        "id_parcelamento": None,
        "id_recorrencia": id_recorrencia,
        "parcela_atual": None,
        "parcelas_totais": None,
    }


def _lookup_recorrente(conn, conta: str, nome: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, categoria, subcategoria, descricao
            from dim_lancamentos_recorrentes
            where conta = %s and nome_lancamento = %s and ativo = true
            """,
            (conta, nome),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "categoria": row[1],
            "subcategoria": row[2], "descricao": row[3]}
