"""Loading, canonicalising and hashing data contracts."""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT

CONTRACT_DIR = ROOT / "contracts"


@dataclass
class ContractColumn:
    name: str
    type: str
    nullable: bool
    description: str = ""
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def signature(self) -> str:
        """Human readable type signature, used in drift messages."""
        if self.type == "TEXT" and self.length:
            return f"TEXT({self.length})"
        if self.type == "NUMBER" and self.precision is not None:
            return f"NUMBER({self.precision},{self.scale})"
        return self.type


@dataclass
class Contract:
    dataset: str                 # e.g. RAW.AP_INVOICE
    version: int
    owner: str
    classification: str
    description: str
    primary_key: list[str]
    freshness: dict
    columns: list[ContractColumn]
    source_path: Path | None = None
    raw: dict = field(default_factory=dict)

    @property
    def schema(self) -> str:
        return self.dataset.split(".")[0]

    @property
    def table(self) -> str:
        return self.dataset.split(".")[1]

    def column(self, name: str) -> ContractColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    def canonical(self) -> dict:
        """Ordering and defaults normalised, so the hash only moves on real change."""
        return {
            "dataset": self.dataset,
            "version": self.version,
            "owner": self.owner,
            "classification": self.classification,
            "primary_key": sorted(self.primary_key),
            "freshness": dict(sorted(self.freshness.items())),
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "length": c.length,
                    "precision": c.precision,
                    "scale": c.scale,
                    "nullable": c.nullable,
                }
                for c in sorted(self.columns, key=lambda c: c.name)
            ],
        }

    def spec_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def parse_contract(path: Path) -> Contract:
    data = yaml.safe_load(path.read_text())
    missing = [k for k in ("dataset", "version", "columns") if k not in data]
    if missing:
        raise ValueError(f"{path.name}: missing required keys {missing}")
    cols = [
        ContractColumn(
            name=c["name"],
            type=c["type"],
            nullable=bool(c.get("nullable", True)),
            description=c.get("description", ""),
            length=c.get("length"),
            precision=c.get("precision"),
            scale=c.get("scale"),
        )
        for c in data["columns"]
    ]
    names = [c.name for c in cols]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"{path.name}: duplicate columns {sorted(dupes)}")
    pk = data.get("primary_key", [])
    unknown_pk = [k for k in pk if k not in names]
    if unknown_pk:
        raise ValueError(f"{path.name}: primary_key references unknown columns {unknown_pk}")
    return Contract(
        dataset=data["dataset"].upper(),
        version=int(data["version"]),
        owner=data.get("owner", "unassigned"),
        classification=data.get("classification", "internal"),
        description=data.get("description", ""),
        primary_key=pk,
        freshness=data.get("freshness", {}),
        columns=cols,
        source_path=path,
        raw=data,
    )


def load_contracts(directory: Path | None = None) -> list[Contract]:
    d = directory or CONTRACT_DIR
    paths = sorted(d.rglob("*.yml")) + sorted(d.rglob("*.yaml"))
    return [parse_contract(p) for p in paths]
