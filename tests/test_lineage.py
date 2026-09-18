"""Impact analysis, against a hand written manifest.

No dbt run, no warehouse. The fixture below is the shape dbt actually emits,
reduced to one source, two staging models, one mart and two tests.
"""
import json

import pytest

from control.lineage import EXACT, WILDCARD, Lineage

SRC = "source.p.raw.AP_PAYMENT"
OTHER_SRC = "source.p.raw.AP_INVOICE"


def node(name, schema, sql, rtype="model", column=None):
    n = {"name": name, "resource_type": rtype, "schema": schema, "raw_code": sql}
    if column:
        n["column_name"] = column
    return n


MANIFEST = {
    "sources": {
        SRC: {"name": "AP_PAYMENT", "source_name": "raw"},
        OTHER_SRC: {"name": "AP_INVOICE", "source_name": "raw"},
    },
    "nodes": {
        "model.p.stg_ap_payment": node(
            "stg_ap_payment", "STAGING",
            "select payment_id, invoice_id, paid_amount, bank_ref from {{ source('raw','AP_PAYMENT') }}"),
        "model.p.stg_ap_invoice": node(
            "stg_ap_invoice", "STAGING",
            "select invoice_id, gross_amount from {{ source('raw','AP_INVOICE') }}"),
        "model.p.fct_ap_open_items": node(
            "fct_ap_open_items", "MARTS",
            "select * from {{ ref('stg_ap_invoice') }} join {{ ref('stg_ap_payment') }} using (invoice_id)"),
        "model.p.agg_ap_aging": node(
            "agg_ap_aging", "MARTS", "select entity_code from {{ ref('fct_ap_open_items') }}"),
        "test.p.unique_payment_id": node(
            "unique_payment_id", "STAGING", "", rtype="test", column="PAYMENT_ID"),
        "test.p.not_null_bank_ref": node(
            "not_null_bank_ref", "STAGING", "", rtype="test", column="BANK_REF"),
    },
    "child_map": {
        SRC: ["model.p.stg_ap_payment", "test.p.not_null_bank_ref"],
        OTHER_SRC: ["model.p.stg_ap_invoice"],
        "model.p.stg_ap_payment": ["model.p.fct_ap_open_items", "test.p.unique_payment_id"],
        "model.p.stg_ap_invoice": ["model.p.fct_ap_open_items"],
        "model.p.fct_ap_open_items": ["model.p.agg_ap_aging"],
        "model.p.agg_ap_aging": [],
    },
}


@pytest.fixture
def lg():
    return Lineage(MANIFEST)


def test_a_missing_manifest_says_so_rather_than_claiming_no_impact(tmp_path):
    lg = Lineage.load(tmp_path / "nope.json")
    assert not lg.available
    i = lg.impact("RAW.AP_PAYMENT", "BANK_REF")
    assert i.manifest_seen is False
    assert "unknown" in i.summary()
    assert "no dbt manifest" in i.markdown()


def test_a_corrupt_manifest_is_treated_as_missing(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{not json")
    assert not Lineage.load(p).available


def test_dataset_level_impact_reaches_every_descendant(lg):
    i = lg.impact("RAW.AP_PAYMENT")
    assert i.models == ["stg_ap_payment"]
    assert i.marts == ["agg_ap_aging", "fct_ap_open_items"]
    assert "unique_payment_id" in i.tests


def test_column_impact_follows_only_the_paths_that_carry_it(lg):
    i = lg.impact("RAW.AP_PAYMENT", "BANK_REF")
    assert i.models == ["stg_ap_payment"]
    assert i.confidence == EXACT
    # the mart selects *, so it inherits the column
    assert "fct_ap_open_items" in i.marts


def test_a_column_no_model_reads_has_no_model_impact(lg):
    """PAYMENT_METHOD is in the source but no staging model selects it."""
    i = lg.impact("RAW.AP_PAYMENT", "PAYMENT_METHOD")
    assert i.models == []
    assert i.marts == []


def test_a_wildcard_select_is_flagged_as_lower_confidence():
    m = json.loads(json.dumps(MANIFEST))
    m["nodes"]["model.p.stg_ap_payment"]["raw_code"] = \
        "select * from {{ source('raw','AP_PAYMENT') }}"
    i = Lineage(m).impact("RAW.AP_PAYMENT", "BANK_REF")
    assert i.confidence == WILDCARD
    assert i.models == ["stg_ap_payment"]


def test_a_column_scoped_test_on_the_source_is_included(lg):
    assert "not_null_bank_ref" in lg.impact("RAW.AP_PAYMENT", "BANK_REF").tests
    assert "not_null_bank_ref" not in lg.impact("RAW.AP_PAYMENT", "PAID_AMOUNT").tests


def test_an_unknown_dataset_has_no_impact(lg):
    i = lg.impact("RAW.AP_ACCRUAL")
    assert i.empty
    assert i.manifest_seen is True
    assert i.summary() == "nothing downstream"


def test_summary_and_markdown_are_readable(lg):
    i = lg.impact("RAW.AP_PAYMENT", "BANK_REF")
    assert "mart" in i.summary()
    md = i.markdown()
    assert "Marts affected" in md and "fct_ap_open_items" in md
    assert "Column attribution" in md


def test_impact_serialises_for_storage(lg):
    d = lg.impact("RAW.AP_PAYMENT", "BANK_REF").as_dict()
    assert json.loads(json.dumps(d))["marts"]


# --------------------------------------------------------------------------
# exposures: what people actually open
# --------------------------------------------------------------------------

def _with_exposure():
    m = json.loads(json.dumps(MANIFEST))
    m["exposures"] = {
        "exposure.p.ap_aging_pack": {
            "name": "ap_aging_pack", "label": "AP Aging Pack", "type": "dashboard",
            "owner": {"name": "AP Controller", "email": "ap@x"},
            "depends_on": {"nodes": ["model.p.agg_ap_aging"]},
        }
    }
    m["child_map"]["model.p.agg_ap_aging"] = ["exposure.p.ap_aging_pack"]
    return Lineage(m)


def test_an_exposure_downstream_of_a_mart_is_reported():
    i = _with_exposure().impact("RAW.AP_PAYMENT", "BANK_REF")
    assert [r["label"] for r in i.reports] == ["AP Aging Pack"]
    assert i.reports[0]["owner"] == "AP Controller"
    assert "1 report" in i.summary()


def test_reports_lead_the_markdown():
    md = _with_exposure().impact("RAW.AP_PAYMENT", "BANK_REF").markdown()
    assert md.index("Reports affected") < md.index("Marts affected")
    assert "AP Aging Pack (AP Controller)" in md


def test_a_column_that_never_reaches_the_mart_hits_no_report():
    i = _with_exposure().impact("RAW.AP_PAYMENT", "PAYMENT_METHOD")
    assert i.reports == []
