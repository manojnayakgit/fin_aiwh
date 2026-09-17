select
    receipt_id,
    invoice_id,
    receipt_date,
    upper(currency_code)   as currency_code,
    received_amount,
    upper(payment_method)  as payment_method,
    bank_ref,
    loaded_at
from {{ source('raw', 'AR_RECEIPT') }}
