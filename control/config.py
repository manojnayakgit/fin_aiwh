import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    account: str
    user: str
    private_key_path: Path
    private_key_passphrase: str | None
    role: str
    warehouse: str
    database: str
    schema: str

    @property
    def raw_schema(self) -> str:
        return "RAW"


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return v


def load_settings() -> Settings:
    key_path = Path(_req("SNOWFLAKE_PRIVATE_KEY_PATH"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    if not key_path.exists():
        raise SystemExit(f"private key not found at {key_path}")
    return Settings(
        account=_req("SNOWFLAKE_ACCOUNT"),
        user=_req("SNOWFLAKE_USER"),
        private_key_path=key_path,
        private_key_passphrase=os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None,
        role=os.getenv("SNOWFLAKE_ROLE", "FIN_AIWH_ENG"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "FIN_AIWH_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "FIN_AIWH"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "META"),
    )
