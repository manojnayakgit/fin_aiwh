select
    payment_id,
    invoice_id,
    payment_date,
    upper(currency_code)   as currency_code,
    paid_amount,
    upper(payment_method)  as payment_method,
    null::varchar(64) as bank_ref,  -- shield: BANK_REF dropped upstream, restored as NULL, see https://github.com/manojnayakgit/fin_aiwh/issues/3
    loaded_at
from {{ source('raw', 'AP_PAYMENT') }}
