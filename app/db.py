"""Conexao com Postgres via pool."""
import os
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
)


def init_pool():
    pool.open()


def close_pool():
    pool.close()
