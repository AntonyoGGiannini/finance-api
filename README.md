# Finance API

API FastAPI que recebe faturas de cartao e extratos bancarios, aplica a logica de
categorizacao (lookup em dim_lancamentos_recorrentes/parcelados/categoria_lancamento),
chama Claude (Haiku) como fallback quando nao ha match e insere em fact_lancamento
com idempotencia (ON CONFLICT DO NOTHING).

## Estrutura

```
finance-api/
├── app/
│   ├── main.py                       # endpoints FastAPI
│   ├── db.py                         # pool psycopg
│   ├── fmt.py                        # parse_parcelas
│   ├── llm.py                        # cliente Anthropic (sugestao de categoria)
│   ├── categorizacao_fatura.py       # logica de fatura
│   └── categorizacao_extrato.py      # logica de extrato
├── requirements.txt
├── Dockerfile
└── README.md
```

## Deploy no Railway

1. Cria repo no GitHub com esses arquivos.
2. Em railway.app: New Project -> Deploy from GitHub Repo.
3. Em Variables, adiciona:
   - `DATABASE_URL`: connection string Session Pooler do Supabase (mesma que usa no n8n)
   - `ANTHROPIC_API_KEY`: sua chave do console.anthropic.com
4. Railway detecta o Dockerfile e faz deploy automatico.
5. Em Settings -> Networking -> Generate Domain. Copia a URL publica.

## Endpoints

- `GET /health` -> `{"status":"ok"}`
- `POST /processar-fatura` (multipart/form-data)
  - `arquivo`: CSV ou XLSX com colunas `data, lançamento, valor`
  - `conta`: nome exato (ex: `CCM ITAU`)
  - `data_vencimento`: YYYY-MM-DD
- `POST /processar-extrato` (multipart/form-data)
  - `arquivo`: CSV Itau (header ausente, separador `;`, UTF-8 BOM)
  - `conta`: nome exato (ex: `AGG ITAU`)

Resposta:
```json
{
  "inseridos": 15,
  "ignorados": 2,
  "sugestoes_pendentes": 3,
  "lancamentos": [...]
}
```

## Migration necessaria no Postgres

```sql
alter table fact_lancamento
  add column revisado boolean default true,
  add column contexto smallint,
  add column origem_categoria text default 'historico';

update fact_lancamento set revisado = true where revisado is null;
```

## Lancamentos pendentes de revisao

Filtrar no Supabase Table Editor:

```sql
select * from fact_lancamento
where revisado = false
order by data_lancamento desc;
```
