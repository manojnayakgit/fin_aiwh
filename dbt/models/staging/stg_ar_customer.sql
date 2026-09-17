select
    customer_id,
    trim(customer_name)                    as customer_name,
    upper(nullif(trim(country_code), ''))  as country_code,
    credit_limit,
    upper(nullif(trim(payment_terms), '')) as payment_terms,
    is_active,
    created_at,
    loaded_at
from {{ source('raw', 'AR_CUSTOMER') }}
