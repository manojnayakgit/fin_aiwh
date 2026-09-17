-- Undo every scenario. Rebuilds RAW to the version 1 shape and drops the
-- ungoverned table. Reload data afterwards:
--   python -m control.cli load
--   python -m control.cli resolve --all
DROP TABLE IF EXISTS FIN_AIWH.RAW.AP_ACCRUAL;

-- One time cleanup: an earlier version of this file ran unqualified DDL and
-- created empty copies of the RAW tables in META. Harmless to repeat.
DROP TABLE IF EXISTS FIN_AIWH.META.AP_VENDOR;
DROP TABLE IF EXISTS FIN_AIWH.META.AP_INVOICE;
DROP TABLE IF EXISTS FIN_AIWH.META.AP_INVOICE_LINE;
DROP TABLE IF EXISTS FIN_AIWH.META.AP_PAYMENT;
DROP TABLE IF EXISTS FIN_AIWH.META.AR_CUSTOMER;
DROP TABLE IF EXISTS FIN_AIWH.META.AR_INVOICE;
DROP TABLE IF EXISTS FIN_AIWH.META.AR_RECEIPT;
DROP TABLE IF EXISTS FIN_AIWH.META.FX_RATE;


CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_VENDOR (
    VENDOR_ID      VARCHAR(32)   NOT NULL  COMMENT 'Natural key from the ERP vendor master.',
    VENDOR_NAME    VARCHAR(200)  NOT NULL  COMMENT 'Legal name as registered.',
    COUNTRY_CODE   VARCHAR(2)     COMMENT 'ISO 3166-1 alpha-2 of the vendor''s registered address.',
    PAYMENT_TERMS  VARCHAR(16)    COMMENT 'Terms code, for example NET30.',
    TAX_ID         VARCHAR(64)    COMMENT 'Tax registration number. Restricted.',
    IS_ACTIVE      BOOLEAN       NOT NULL  COMMENT 'False once the vendor is blocked for payment.',
    CREATED_AT     TIMESTAMP_NTZ NOT NULL  COMMENT 'Vendor record creation time in the source system.',
    LOADED_AT      TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Vendor master from the source ERP.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate invoice key from the ERP.',
    VENDOR_ID       VARCHAR(32)   NOT NULL  COMMENT 'References AP_VENDOR.VENDOR_ID.',
    ENTITY_CODE     VARCHAR(8)    NOT NULL  COMMENT 'Legal entity booking the liability.',
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL  COMMENT 'Vendor''s own invoice number.',
    INVOICE_DATE    DATE          NOT NULL  COMMENT 'Date on the invoice document.',
    DUE_DATE        DATE          NOT NULL  COMMENT 'Contractual payment due date.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 transaction currency.',
    GROSS_AMOUNT    NUMBER(18,2)  NOT NULL  COMMENT 'Invoice total including tax, transaction currency.',
    TAX_AMOUNT      NUMBER(18,2)   COMMENT 'Tax portion of the gross amount.',
    STATUS          VARCHAR(16)   NOT NULL  COMMENT 'OPEN, PAID, PARTIAL, CANCELLED, DISPUTED.',
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL  COMMENT 'Originating ERP instance.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts payable invoice header.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_INVOICE_LINE (
    LINE_ID      VARCHAR(40)   NOT NULL  COMMENT 'Surrogate line key.',
    INVOICE_ID   VARCHAR(32)   NOT NULL  COMMENT 'References AP_INVOICE.INVOICE_ID.',
    LINE_NUMBER  NUMBER(9,0)   NOT NULL  COMMENT 'Line sequence within the invoice.',
    COST_CENTER  VARCHAR(16)    COMMENT 'Cost centre charged.',
    GL_ACCOUNT   VARCHAR(16)   NOT NULL  COMMENT 'GL account the line posts to.',
    LINE_AMOUNT  NUMBER(18,2)  NOT NULL  COMMENT 'Line net amount, transaction currency.',
    DESCRIPTION  VARCHAR(500)   COMMENT 'Free text line description.',
    LOADED_AT    TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts payable invoice line detail with GL coding.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AP_PAYMENT (
    PAYMENT_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate payment key.',
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'References AP_INVOICE.INVOICE_ID.',
    PAYMENT_DATE    DATE          NOT NULL  COMMENT 'Value date of the payment.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 payment currency.',
    PAID_AMOUNT     NUMBER(18,2)  NOT NULL  COMMENT 'Amount applied, payment currency.',
    PAYMENT_METHOD  VARCHAR(16)    COMMENT 'ACH, WIRE, CHECK, SEPA.',
    BANK_REF        VARCHAR(64)    COMMENT 'Bank reference for reconciliation.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Payments applied against AP invoices.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AR_CUSTOMER (
    CUSTOMER_ID    VARCHAR(32)   NOT NULL  COMMENT 'Natural key from the ERP customer master.',
    CUSTOMER_NAME  VARCHAR(200)  NOT NULL  COMMENT 'Legal name as registered.',
    COUNTRY_CODE   VARCHAR(2)     COMMENT 'ISO 3166-1 alpha-2 of the billing address.',
    CREDIT_LIMIT   NUMBER(18,2)   COMMENT 'Approved credit limit, reporting currency.',
    PAYMENT_TERMS  VARCHAR(16)    COMMENT 'Terms code, for example NET45.',
    IS_ACTIVE      BOOLEAN       NOT NULL  COMMENT 'False once the customer is on credit hold.',
    CREATED_AT     TIMESTAMP_NTZ NOT NULL  COMMENT 'Customer record creation time in the source system.',
    LOADED_AT      TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Customer master from the source ERP.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AR_INVOICE (
    INVOICE_ID      VARCHAR(32)   NOT NULL  COMMENT 'Surrogate invoice key from the ERP.',
    CUSTOMER_ID     VARCHAR(32)   NOT NULL  COMMENT 'References AR_CUSTOMER.CUSTOMER_ID.',
    ENTITY_CODE     VARCHAR(8)    NOT NULL  COMMENT 'Legal entity recognising the receivable.',
    INVOICE_NUMBER  VARCHAR(64)   NOT NULL  COMMENT 'Our invoice number issued to the customer.',
    INVOICE_DATE    DATE          NOT NULL  COMMENT 'Date on the invoice document.',
    DUE_DATE        DATE          NOT NULL  COMMENT 'Contractual payment due date.',
    CURRENCY_CODE   VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 transaction currency.',
    GROSS_AMOUNT    NUMBER(18,2)  NOT NULL  COMMENT 'Invoice total including tax, transaction currency.',
    TAX_AMOUNT      NUMBER(18,2)   COMMENT 'Tax portion of the gross amount.',
    STATUS          VARCHAR(16)   NOT NULL  COMMENT 'OPEN, PAID, PARTIAL, CANCELLED, DISPUTED.',
    SOURCE_SYSTEM   VARCHAR(32)   NOT NULL  COMMENT 'Originating ERP instance.',
    LOADED_AT       TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Accounts receivable invoice header.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.AR_RECEIPT (
    RECEIPT_ID       VARCHAR(32)   NOT NULL  COMMENT 'Surrogate receipt key.',
    INVOICE_ID       VARCHAR(32)   NOT NULL  COMMENT 'References AR_INVOICE.INVOICE_ID.',
    RECEIPT_DATE     DATE          NOT NULL  COMMENT 'Value date of the receipt.',
    CURRENCY_CODE    VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 receipt currency.',
    RECEIVED_AMOUNT  NUMBER(18,2)  NOT NULL  COMMENT 'Amount applied, receipt currency.',
    PAYMENT_METHOD   VARCHAR(16)    COMMENT 'ACH, WIRE, CHECK, SEPA.',
    BANK_REF         VARCHAR(64)    COMMENT 'Bank reference for reconciliation.',
    LOADED_AT        TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Cash receipts applied against AR invoices.';

CREATE OR REPLACE TABLE FIN_AIWH.RAW.FX_RATE (
    RATE_DATE      DATE          NOT NULL  COMMENT 'Rate effective date.',
    FROM_CURRENCY  VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 source currency.',
    TO_CURRENCY    VARCHAR(3)    NOT NULL  COMMENT 'ISO 4217 target currency.',
    RATE           NUMBER(18,8)  NOT NULL  COMMENT 'Units of target per one unit of source.',
    RATE_TYPE      VARCHAR(16)   NOT NULL  COMMENT 'SPOT, CLOSING, AVERAGE.',
    LOADED_AT      TIMESTAMP_NTZ NOT NULL  COMMENT 'Ingestion watermark.'
) COMMENT = 'Daily FX rates used to translate subledger amounts to reporting currency.';
