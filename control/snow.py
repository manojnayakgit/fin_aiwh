from contextlib import contextmanager

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from .config import Settings, load_settings


def _private_key_der(settings: Settings) -> bytes:
    pwd = (
        settings.private_key_passphrase.encode()
        if settings.private_key_passphrase
        else None
    )
    with open(settings.private_key_path, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(), password=pwd, backend=default_backend()
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@contextmanager
def connect(settings: Settings | None = None, schema: str | None = None):
    s = settings or load_settings()
    conn = snowflake.connector.connect(
        account=s.account,
        user=s.user,
        private_key=_private_key_der(s),
        role=s.role,
        warehouse=s.warehouse,
        database=s.database,
        schema=schema or s.schema,
        client_session_keep_alive=False,
    )
    try:
        yield conn
    finally:
        conn.close()


def query(conn, sql: str, params: tuple | dict | None = None) -> list[dict]:
    cur = conn.cursor(snowflake.connector.DictCursor)
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def execute(conn, sql: str, params: tuple | dict | None = None) -> None:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
    finally:
        cur.close()


def execute_script(conn, sql_text: str) -> int:
    """Run a multi statement SQL file. Returns the number of statements run."""
    count = 0
    for cur in conn.execute_string(sql_text):
        cur.close()
        count += 1
    return count
