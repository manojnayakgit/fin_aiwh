"""Single source of truth for the v1 RAW layer.

Used to emit the RAW DDL and the v1 data contracts so the two start in sync.
After day one they diverge on purpose: DDL is changed by upstream teams,
contracts are changed only by a reviewed pull request. The gap between them
is what the detector reports.
"""

# (name, snowflake_type, nullable, description)
DATASETS = {
    "AP_VENDOR": dict(
        description="Vendor master from the source ERP.",
        primary_key=["VENDOR_ID"],
        columns=[
            ("VENDOR_ID",      "VARCHAR(32)",   False, "Natural key from the ERP vendor master."),
            ("VENDOR_NAME",    "VARCHAR(200)",  False, "Legal name as registered."),
            ("COUNTRY_CODE",   "VARCHAR(2)",    True,  "ISO 3166-1 alpha-2 of the vendor's registered address."),
            ("PAYMENT_TERMS",  "VARCHAR(16)",   True,  "Terms code, for example NET30."),
            ("TAX_ID",         "VARCHAR(64)",   True,  "Tax registration number. Restricted."),
            ("IS_ACTIVE",      "BOOLEAN",       False, "False once the vendor is blocked for payment."),
            ("CREATED_AT",     "TIMESTAMP_NTZ", False, "Vendor record creation time in the source system."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "AP_INVOICE": dict(
        description="Accounts payable invoice header.",
        primary_key=["INVOICE_ID"],
        columns=[
            ("INVOICE_ID",     "VARCHAR(32)",   False, "Surrogate invoice key from the ERP."),
            ("VENDOR_ID",      "VARCHAR(32)",   False, "References AP_VENDOR.VENDOR_ID."),
            ("ENTITY_CODE",    "VARCHAR(8)",    False, "Legal entity booking the liability."),
            ("INVOICE_NUMBER", "VARCHAR(64)",   False, "Vendor's own invoice number."),
            ("INVOICE_DATE",   "DATE",          False, "Date on the invoice document."),
            ("DUE_DATE",       "DATE",          False, "Contractual payment due date."),
            ("CURRENCY_CODE",  "VARCHAR(3)",    False, "ISO 4217 transaction currency."),
            ("GROSS_AMOUNT",   "NUMBER(18,2)",  False, "Invoice total including tax, transaction currency."),
            ("TAX_AMOUNT",     "NUMBER(18,2)",  True,  "Tax portion of the gross amount."),
            ("STATUS",         "VARCHAR(16)",   False, "OPEN, PAID, PARTIAL, CANCELLED, DISPUTED."),
            ("SOURCE_SYSTEM",  "VARCHAR(32)",   False, "Originating ERP instance."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "AP_INVOICE_LINE": dict(
        description="Accounts payable invoice line detail with GL coding.",
        primary_key=["LINE_ID"],
        columns=[
            ("LINE_ID",        "VARCHAR(40)",   False, "Surrogate line key."),
            ("INVOICE_ID",     "VARCHAR(32)",   False, "References AP_INVOICE.INVOICE_ID."),
            ("LINE_NUMBER",    "NUMBER(9,0)",   False, "Line sequence within the invoice."),
            ("COST_CENTER",    "VARCHAR(16)",   True,  "Cost centre charged."),
            ("GL_ACCOUNT",     "VARCHAR(16)",   False, "GL account the line posts to."),
            ("LINE_AMOUNT",    "NUMBER(18,2)",  False, "Line net amount, transaction currency."),
            ("DESCRIPTION",    "VARCHAR(500)",  True,  "Free text line description."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "AP_PAYMENT": dict(
        description="Payments applied against AP invoices.",
        primary_key=["PAYMENT_ID"],
        columns=[
            ("PAYMENT_ID",     "VARCHAR(32)",   False, "Surrogate payment key."),
            ("INVOICE_ID",     "VARCHAR(32)",   False, "References AP_INVOICE.INVOICE_ID."),
            ("PAYMENT_DATE",   "DATE",          False, "Value date of the payment."),
            ("CURRENCY_CODE",  "VARCHAR(3)",    False, "ISO 4217 payment currency."),
            ("PAID_AMOUNT",    "NUMBER(18,2)",  False, "Amount applied, payment currency."),
            ("PAYMENT_METHOD", "VARCHAR(16)",   True,  "ACH, WIRE, CHECK, SEPA."),
            ("BANK_REF",       "VARCHAR(64)",   True,  "Bank reference for reconciliation."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "AR_CUSTOMER": dict(
        description="Customer master from the source ERP.",
        primary_key=["CUSTOMER_ID"],
        columns=[
            ("CUSTOMER_ID",    "VARCHAR(32)",   False, "Natural key from the ERP customer master."),
            ("CUSTOMER_NAME",  "VARCHAR(200)",  False, "Legal name as registered."),
            ("COUNTRY_CODE",   "VARCHAR(2)",    True,  "ISO 3166-1 alpha-2 of the billing address."),
            ("CREDIT_LIMIT",   "NUMBER(18,2)",  True,  "Approved credit limit, reporting currency."),
            ("PAYMENT_TERMS",  "VARCHAR(16)",   True,  "Terms code, for example NET45."),
            ("IS_ACTIVE",      "BOOLEAN",       False, "False once the customer is on credit hold."),
            ("CREATED_AT",     "TIMESTAMP_NTZ", False, "Customer record creation time in the source system."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "AR_INVOICE": dict(
        description="Accounts receivable invoice header.",
        primary_key=["INVOICE_ID"],
        columns=[
            ("INVOICE_ID",     "VARCHAR(32)",   False, "Surrogate invoice key from the ERP."),
            ("CUSTOMER_ID",    "VARCHAR(32)",   False, "References AR_CUSTOMER.CUSTOMER_ID."),
            ("ENTITY_CODE",    "VARCHAR(8)",    False, "Legal entity recognising the receivable."),
            ("INVOICE_NUMBER", "VARCHAR(64)",   False, "Our invoice number issued to the customer."),
            ("INVOICE_DATE",   "DATE",          False, "Date on the invoice document."),
            ("DUE_DATE",       "DATE",          False, "Contractual payment due date."),
            ("CURRENCY_CODE",  "VARCHAR(3)",    False, "ISO 4217 transaction currency."),
            ("GROSS_AMOUNT",   "NUMBER(18,2)",  False, "Invoice total including tax, transaction currency."),
            ("TAX_AMOUNT",     "NUMBER(18,2)",  True,  "Tax portion of the gross amount."),
            ("STATUS",         "VARCHAR(16)",   False, "OPEN, PAID, PARTIAL, CANCELLED, DISPUTED."),
            ("SOURCE_SYSTEM",  "VARCHAR(32)",   False, "Originating ERP instance."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "AR_RECEIPT": dict(
        description="Cash receipts applied against AR invoices.",
        primary_key=["RECEIPT_ID"],
        columns=[
            ("RECEIPT_ID",     "VARCHAR(32)",   False, "Surrogate receipt key."),
            ("INVOICE_ID",     "VARCHAR(32)",   False, "References AR_INVOICE.INVOICE_ID."),
            ("RECEIPT_DATE",   "DATE",          False, "Value date of the receipt."),
            ("CURRENCY_CODE",  "VARCHAR(3)",    False, "ISO 4217 receipt currency."),
            ("RECEIVED_AMOUNT","NUMBER(18,2)",  False, "Amount applied, receipt currency."),
            ("PAYMENT_METHOD", "VARCHAR(16)",   True,  "ACH, WIRE, CHECK, SEPA."),
            ("BANK_REF",       "VARCHAR(64)",   True,  "Bank reference for reconciliation."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
    "FX_RATE": dict(
        description="Daily FX rates used to translate subledger amounts to reporting currency.",
        primary_key=["RATE_DATE", "FROM_CURRENCY", "TO_CURRENCY", "RATE_TYPE"],
        columns=[
            ("RATE_DATE",      "DATE",          False, "Rate effective date."),
            ("FROM_CURRENCY",  "VARCHAR(3)",    False, "ISO 4217 source currency."),
            ("TO_CURRENCY",    "VARCHAR(3)",    False, "ISO 4217 target currency."),
            ("RATE",           "NUMBER(18,8)",  False, "Units of target per one unit of source."),
            ("RATE_TYPE",      "VARCHAR(16)",   False, "SPOT, CLOSING, AVERAGE."),
            ("LOADED_AT",      "TIMESTAMP_NTZ", False, "Ingestion watermark."),
        ],
    ),
}

OWNER = "finance-data-engineering"
CLASSIFICATION = "financial-restricted"
