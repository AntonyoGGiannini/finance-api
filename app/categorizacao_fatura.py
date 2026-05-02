"""Categorizacao de fatura de cartao.

Replica a logica de adicionar_fatura() do Streamlit em SQL puro.
Sequencia de tentativas de categorizacao (parar na primeira que casa):
  1. Eh parcelado -> busca em dim_lancamentos_parcelados
  2. Nao parcelado -> busca recorrencia em dim_lancamentos_recorrentes (Credito)
  3. Nao recorrente -> busca em dim_categoria_lancamento
  4. Sem match -> chama LLM (estrategia B) e marca revisado=false
"""
import hashlib
import re

from .db import pool
from .fmt import parse_parcelas
from .llm import sugerir_categoria


def _normalizar_nome(nome: str) -> str:
    nome = re.sub(r"\s+", " ", nome).strip().upper()
    nome = nome.replace("SAO PAULO BRA", "").strip()
    if nome[:18] == "ANUIDADE DIFERENCI":
        nome = "ANUIDADE DIFERENCI"
    return nome


def _gerar_id_lancamento(
    data_lancamento: str,
    data_vencimento: str,
    conta: str,
    tipo: str,
    funcao: str,
    nome: str,
    valor: float,
    linha: int,
) -> str:
    raw = f"{data_lancamento}|{data_vencimento}|{conta}|{tipo}|{funcao}|{nome}|{valor}|{linha}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _gerar_id_parcelamento(
    data: str, conta: str, nome: str, valor: float, parcelas_totais: int
) -> str:
    raw = f"{data}|{conta}|{nome}|{valor}|{parcelas_totais}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _carregar_taxonomia(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "select distinct categoria, coalesce(subcategoria, '') "
            "from dim_categoria order by 1, 2"
        )
        return cur.fetchall()


def processar_fatura(
    linhas: list[dict],
    conta: str,
    data_vencimento: str,
) -> dict:
    inseridos = 0
    ignorados = 0
    pendentes = 0
    detalhes = []

    # FASE 1: parse + lookup determinístico, acumula registros
    registros = []  # cada um: dict com tudo que precisa pra inserir + flag 'precisa_llm'

    with pool.connection() as conn:
        taxonomia = _carregar_taxonomia(conn)

        for r, item in enumerate(linhas, start=1):
            try:
                data_lancamento = item["data"]
                nome_raw = str(item["lancamento"])
                valor_lancamento = float(item["valor"])
            except (KeyError, ValueError, TypeError):
                ignorados += 1
                continue

            nome_normalizado = _normalizar_nome(nome_raw)
            if nome_normalizado in ("PAGAMENTO EFETUADO", "CONTROLE DE SALDO"):
                ignorados += 1
                continue

            if valor_lancamento < 0:
                tipo = "Receita"
                valor_lancamento = abs(valor_lancamento)
            else:
                tipo = "Despesa"
            funcao = "Crédito"

            parcelado, p, pt, nome_lancamento = parse_parcelas(nome_normalizado)

            categoria = ""
            subcategoria = ""
            descricao = ""
            classe = "Variável"
            id_parcelamento = None
            id_recorrencia = None
            contexto = None

            if parcelado == "S" and nome_lancamento != "ANUIDADE DIFERENCI":
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id, categoria, coalesce(subcategoria, ''),
                               coalesce(descricao, '')
                        from dim_lancamentos_parcelados
                        where conta = %s and nome_lancamento = %s
                          and data_lancamento = %s and parcelas_totais = %s
                          and valor between %s - 0.05 and %s + 0.05
                        limit 1
                        """,
                        (conta, nome_lancamento, data_lancamento, pt,
                         valor_lancamento, valor_lancamento),
                    )
                    row = cur.fetchone()
                if row:
                    id_parcelamento, categoria, subcategoria, descricao = row
                    contexto = 1
                else:
                    id_parcelamento = _gerar_id_parcelamento(
                        data_lancamento, conta, nome_lancamento, valor_lancamento, pt
                    )
                    contexto = 2
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id, categoria, coalesce(subcategoria, ''),
                               coalesce(descricao, '')
                        from dim_lancamentos_recorrentes
                        where conta = %s and nome_lancamento = %s and valor = %s
                        limit 1
                        """,
                        (conta, nome_lancamento, valor_lancamento),
                    )
                    row = cur.fetchone()
                if row:
                    classe = "Fixa"
                    id_recorrencia, categoria, subcategoria, descricao = row
                    contexto = 5
                else:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            select categoria, coalesce(subcategoria, ''),
                                   coalesce(descricao, '')
                            from dim_categoria_lancamento
                            where conta = %s and nome_lancamento = %s
                              and tipo = 'Despesa' and funcao = 'Crédito'
                            limit 1
                            """,
                            (conta, nome_lancamento),
                        )
                        row = cur.fetchone()
