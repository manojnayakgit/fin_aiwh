select
    vendor_id,
    trim(vendor_name)                     as vendor_name,
    upper(nullif(trim(country_code), '')) as country_code,
    upper(nullif(trim(payment_terms), '')) as payment_terms,
    nullif(trim(tax_id), '')              as tax_id,
    is_active,
    created_at,
    loaded_at
from {{ source('raw', 'AP_VENDOR') }}
