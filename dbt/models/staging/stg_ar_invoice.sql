-- shield: STATUS may now be null upstream, guarded by a test, see https://github.com/manojnayakgit/fin_aiwh/issues/4

select
    invoice_id,
    customer_id,
    entity_code,
    invoice_number,
    invoice_date,
    due_date,
    upper(currency_code)      as currency_code,
    gross_amount,
    coalesce(tax_amount, 0)   as tax_amount,
    gross_amount - coalesce(tax_amount, 0) as net_amount,
    upper(status)             as status,
    source_system,
    loaded_at
from {{ source('raw', 'AR_INVOICE') }}
