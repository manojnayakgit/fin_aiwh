select
    payment_id,
    invoice_id,
    payment_date,
    upper(currency_code)   as currency_code,
    paid_amount,
    upper(payment_method)  as payment_method,
    bank_ref,
    loaded_at
from {{ source('raw', 'AP_PAYMENT') }}
