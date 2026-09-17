"""Generate AP/AR subledger seed data as CSV.

Deterministic: same seed, same data, so a drift run is reproducible. The data is
deliberately imperfect in the ways finance data actually is - partial payments,
disputed invoices, missing cost centres, a handful of near duplicate vendors -
so the quality gates have something real to catch.
"""
import csv
import os
import random
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _spec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "seeds", "out")

SEED = 20260918
rnd = random.Random(SEED)

START = date(2025, 1, 1)
END = date(2026, 9, 1)
DAYS = (END - START).days

ENTITIES = ["UK01", "DE01", "US01", "SG01", "IN01"]
CURRENCIES = ["GBP", "EUR", "USD", "SGD", "INR"]
ENTITY_CCY = dict(zip(ENTITIES, CURRENCIES))
TERMS = ["NET15", "NET30", "NET45", "NET60"]
METHODS = ["ACH", "WIRE", "CHECK", "SEPA"]
COUNTRIES = ["GB", "DE", "US", "SG", "IN", "FR", "NL"]
GL_ACCOUNTS = ["500100", "500200", "510300", "520100", "600400", "610200"]
COST_CENTRES = [f"CC{n:04d}" for n in range(1000, 1012)]

LOADED = datetime(2026, 9, 18, 2, 0, 0)


def d(n):
    return START + timedelta(days=n)


def ts(x: datetime) -> str:
    return x.strftime("%Y-%m-%d %H:%M:%S")


def money(lo, hi):
    return round(rnd.uniform(lo, hi), 2)


def write(name, rows):
    os.makedirs(OUT, exist_ok=True)
    cols = [c[0] for c in _spec.DATASETS[name]["columns"]]
    path = os.path.join(OUT, f"{name}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(cols)
        w.writerows(rows)
    print(f"{name:<18} {len(rows):>7,} rows")


def gen_vendors(n=220):
    rows = []
    bases = ["Northbridge", "Kestrel", "Halden", "Vireo", "Ardent", "Cobalt", "Merton",
             "Pallas", "Quorum", "Saxon", "Tamesis", "Ulster", "Verity", "Whitlock"]
    suffix = ["Ltd", "GmbH", "Inc", "Pte Ltd", "Services", "Holdings", "Group"]
    for i in range(n):
        vid = f"V{100000 + i}"
        name = f"{rnd.choice(bases)} {rnd.choice(suffix)}"
        # a few near duplicates, the kind that survive a bad master data merge
        if i % 47 == 0 and i:
            name = rows[i - 1][1].upper()
        rows.append([
            vid, name,
            rnd.choice(COUNTRIES) if rnd.random() > 0.03 else "",
            rnd.choice(TERMS) if rnd.random() > 0.05 else "",
            f"{rnd.choice(COUNTRIES)}{rnd.randint(10**8, 10**9 - 1)}" if rnd.random() > 0.12 else "",
            "true" if rnd.random() > 0.08 else "false",
            ts(datetime.combine(d(rnd.randint(0, 200)), datetime.min.time())),
            ts(LOADED),
        ])
    return rows


def gen_customers(n=310):
    rows = []
    bases = ["Aldgate", "Brantley", "Calder", "Dunmore", "Eastvale", "Fenwick",
             "Granby", "Hollis", "Inverness", "Jarrow", "Kelsey", "Lyndon"]
    suffix = ["PLC", "AG", "LLC", "Pte Ltd", "Partners", "Retail", "Industries"]
    for i in range(n):
        rows.append([
            f"C{200000 + i}", f"{rnd.choice(bases)} {rnd.choice(suffix)}",
            rnd.choice(COUNTRIES) if rnd.random() > 0.03 else "",
            money(50_000, 5_000_000) if rnd.random() > 0.15 else "",
            rnd.choice(TERMS) if rnd.random() > 0.04 else "",
            "true" if rnd.random() > 0.06 else "false",
            ts(datetime.combine(d(rnd.randint(0, 200)), datetime.min.time())),
            ts(LOADED),
        ])
    return rows


def gen_ap(vendors, n=8000):
    inv, lines, pays = [], [], []
    line_no, pay_no = 0, 0
    for i in range(n):
        iid = f"API{500000 + i}"
        v = rnd.choice(vendors)[0]
        ent = rnd.choice(ENTITIES)
        ccy = ENTITY_CCY[ent] if rnd.random() > 0.2 else rnd.choice(CURRENCIES)
        idate = d(rnd.randint(0, DAYS))
        terms = int(rnd.choice(TERMS)[3:])
        gross = money(200, 250_000)
        tax = round(gross * rnd.choice([0.0, 0.05, 0.19, 0.2]), 2)
        r = rnd.random()
        status = "PAID" if r < 0.62 else "OPEN" if r < 0.85 else "PARTIAL" if r < 0.93 \
            else "DISPUTED" if r < 0.98 else "CANCELLED"
        inv.append([
            iid, v, ent, f"INV-{rnd.randint(10**6, 10**7 - 1)}", idate.isoformat(),
            (idate + timedelta(days=terms)).isoformat(), ccy, gross,
            tax if rnd.random() > 0.07 else "", status, "ERP_SAP_P01", ts(LOADED),
        ])

        net = round(gross - (tax or 0), 2)
        nlines = rnd.randint(1, 4)
        remaining = net
        for ln in range(1, nlines + 1):
            amt = round(remaining if ln == nlines else remaining * rnd.uniform(0.2, 0.6), 2)
            remaining = round(remaining - amt, 2)
            line_no += 1
            lines.append([
                f"APL{900000 + line_no}", iid, ln,
                rnd.choice(COST_CENTRES) if rnd.random() > 0.09 else "",
                rnd.choice(GL_ACCOUNTS), amt,
                f"Line {ln} services rendered" if rnd.random() > 0.3 else "",
                ts(LOADED),
            ])

        if status in ("PAID", "PARTIAL"):
            paid = gross if status == "PAID" else round(gross * rnd.uniform(0.2, 0.8), 2)
            pay_no += 1
            pays.append([
                f"APP{700000 + pay_no}", iid,
                (idate + timedelta(days=terms + rnd.randint(-5, 40))).isoformat(),
                ccy, paid,
                rnd.choice(METHODS) if rnd.random() > 0.05 else "",
                f"BR{rnd.randint(10**9, 10**10 - 1)}" if rnd.random() > 0.1 else "",
                ts(LOADED),
            ])
    return inv, lines, pays


def gen_ar(customers, n=9000):
    inv, recs = [], []
    rec_no = 0
    for i in range(n):
        iid = f"ARI{600000 + i}"
        c = rnd.choice(customers)[0]
        ent = rnd.choice(ENTITIES)
        ccy = ENTITY_CCY[ent] if rnd.random() > 0.25 else rnd.choice(CURRENCIES)
        idate = d(rnd.randint(0, DAYS))
        terms = int(rnd.choice(TERMS)[3:])
        gross = money(500, 400_000)
        tax = round(gross * rnd.choice([0.0, 0.05, 0.19, 0.2]), 2)
        r = rnd.random()
        status = "PAID" if r < 0.58 else "OPEN" if r < 0.84 else "PARTIAL" if r < 0.92 \
            else "DISPUTED" if r < 0.985 else "CANCELLED"
        inv.append([
            iid, c, ent, f"SI-{rnd.randint(10**6, 10**7 - 1)}", idate.isoformat(),
            (idate + timedelta(days=terms)).isoformat(), ccy, gross,
            tax if rnd.random() > 0.06 else "", status, "ERP_SAP_P01", ts(LOADED),
        ])
        if status in ("PAID", "PARTIAL"):
            got = gross if status == "PAID" else round(gross * rnd.uniform(0.15, 0.85), 2)
            rec_no += 1
            recs.append([
                f"ARR{800000 + rec_no}", iid,
                (idate + timedelta(days=terms + rnd.randint(-3, 55))).isoformat(),
                ccy, got,
                rnd.choice(METHODS) if rnd.random() > 0.05 else "",
                f"BR{rnd.randint(10**9, 10**10 - 1)}" if rnd.random() > 0.08 else "",
                ts(LOADED),
            ])
    return inv, recs


def gen_fx():
    rows = []
    base = {"GBP": 1.27, "EUR": 1.09, "SGD": 0.74, "INR": 0.012, "USD": 1.0}
    for n in range(DAYS + 1):
        day = d(n)
        for ccy, start in base.items():
            if ccy == "USD":
                continue
            drift = 1 + (rnd.random() - 0.5) * 0.02
            for rtype in ("SPOT", "CLOSING"):
                rows.append([
                    day.isoformat(), ccy, "USD",
                    round(start * drift * (1.001 if rtype == "CLOSING" else 1.0), 8),
                    rtype, ts(LOADED),
                ])
    return rows


if __name__ == "__main__":
    vendors = gen_vendors()
    customers = gen_customers()
    ap_inv, ap_lines, ap_pay = gen_ap(vendors)
    ar_inv, ar_rec = gen_ar(customers)
    write("AP_VENDOR", vendors)
    write("AR_CUSTOMER", customers)
    write("AP_INVOICE", ap_inv)
    write("AP_INVOICE_LINE", ap_lines)
    write("AP_PAYMENT", ap_pay)
    write("AR_INVOICE", ar_inv)
    write("AR_RECEIPT", ar_rec)
    write("FX_RATE", gen_fx())
    print(f"\nwritten to seeds/out/ (seed={SEED})")
