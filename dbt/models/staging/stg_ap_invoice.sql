select
    invoice_id,
    vendor_id,
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
from {{ source('raw', 'AP_INVOICE') }}
