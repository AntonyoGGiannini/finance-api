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
    """linhas: lista de dicts com chaves 'data', 'lancamento', 'valor' (CSV/Excel ja parseado).
    Retorna: {inseridos, ignorados, sugestoes_pendentes, lancamentos: [...]}.
    """
    inseridos = 0
    ignorados = 0
    pendentes = 0
    detalhes = []

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

            # Cashback / estorno em fatura vem como valor negativo = receita
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
                # Tentativa 1: dim_lancamentos_parcelados
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id, categoria, coalesce(subcategoria, ''),
                               coalesce(descricao, '')
                        from dim_lancamentos_parcelados
                        where conta = %s
                          and nome_lancamento = %s
                          and data_lancamento = %s
                          and parcelas_totais = %s
                          and valor between %s - 0.05 and %s + 0.05
                        limit 1
                        """,
                        (
                            conta,
                            nome_lancamento,
                            data_lancamento,
                            pt,
                            valor_lancamento,
                            valor_lancamento,
                        ),
                    )
                    row = cur.fetchone()

                if row:
                    id_parcelamento, categoria, subcategoria, descricao = row
                    contexto = 1  # parcelado, com categoria
                else:
                    id_parcelamento = _gerar_id_parcelamento(
                        data_lancamento, conta, nome_lancamento, valor_lancamento, pt
                    )
                    contexto = 2  # parcelado, novo
            else:
                # Tentativa 2: dim_lancamentos_recorrentes
                # Filtro de dia mantido fiel ao codigo original (na pratica nao filtra)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id, categoria, coalesce(subcategoria, ''),
                               coalesce(descricao, '')
                        from dim_lancamentos_recorrentes
                        where conta = %s
                          and nome_lancamento = %s
                          and valor = %s
                        limit 1
                        """,
                        (conta, nome_lancamento, valor_lancamento),
                    )
                    row = cur.fetchone()

                if row:
                    classe = "Fixa"
                    id_recorrencia, categoria, subcategoria, descricao = row
                    contexto = 5  # recorrente
                else:
                    # Tentativa 3: dim_categoria_lancamento
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            select categoria, coalesce(subcategoria, ''),
                                   coalesce(descricao, '')
                            from dim_categoria_lancamento
                            where conta = %s
                              and nome_lancamento = %s
                              and tipo = 'Despesa'
                              and funcao = 'Crédito'
                            limit 1
                            """,
                            (conta, nome_lancamento),
                        )
                        row = cur.fetchone()

                    if row:
                        categoria, subcategoria, descricao = row
                        contexto = 4  # historico
                    else:
                        contexto = 3  # vazio - vai pro LLM

            # Estrategia B: chama LLM se ainda nao tem categoria
            origem_categoria = "historico"
            revisado = True
            if not categoria:
                try:
                    sug = sugerir_categoria(
                        nome=nome_lancamento,
                        valor=valor_lancamento,
                        tipo=tipo,
                        funcao=funcao,
                        conta=conta,
                        taxonomia=taxonomia,
                    )
                    categoria = sug["categoria"]
                    subcategoria = sug["subcategoria"]
                    classe = sug["classe"]
                    origem_categoria = "llm"
                    revisado = False
                    pendentes += 1
                except Exception as exc:
                    import traceback
                    print(f"LLM ERROR for {nome_lancamento}: {type(exc).__name__}: {exc}")
                    traceback.print_exc()
                    origem_categoria = "llm_erro"
                    revisado = False

            id_lancamento = _gerar_id_lancamento(
                data_lancamento,
                data_vencimento,
                conta,
                tipo,
                funcao,
                nome_lancamento,
                valor_lancamento,
                r,
            )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into fact_lancamento (
                        id_lancamento, data_lancamento, data_vencimento, conta,
                        tipo, funcao, classe, parcelado, p, pt,
                        nome_lancamento, categoria, subcategoria, descricao,
                        valor_lancamento, id_parcelamento, id_recorrencia,
                        reembolso, contexto, origem_categoria, revisado
                    ) values (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, '0', %s, %s, %s
                    )
                    on conflict (id_lancamento) do nothing
                    returning id_lancamento
                    """,
                    (
                        id_lancamento, data_lancamento, data_vencimento, conta,
                        tipo, funcao, classe, parcelado, p, pt,
                        nome_lancamento, categoria, subcategoria, descricao,
                        valor_lancamento, id_parcelamento, id_recorrencia,
                        contexto, origem_categoria, revisado,
                    ),
                )
                row = cur.fetchone()
                if row:
                    inseridos += 1
                    detalhes.append({
                        "id_lancamento": id_lancamento,
                        "nome": nome_lancamento,
                        "valor": valor_lancamento,
                        "categoria": categoria,
                        "subcategoria": subcategoria,
                        "classe": classe,
                        "contexto": contexto,
                        "origem": origem_categoria,
                        "revisado": revisado,
                    })
                else:
                    ignorados += 1

        conn.commit()

    return {
        "inseridos": inseridos,
        "ignorados": ignorados,
        "sugestoes_pendentes": pendentes,
        "lancamentos": detalhes,
    }
