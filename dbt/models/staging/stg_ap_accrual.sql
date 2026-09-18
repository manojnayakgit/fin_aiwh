select
    accrual_id,
    upper(entity_code)   as entity_code,
    upper(gl_account)    as gl_account,
    period,
    accrual_amount,
    upper(currency_code) as currency_code,
    loaded_at
from {{ source('raw', 'AP_ACCRUAL') }}
