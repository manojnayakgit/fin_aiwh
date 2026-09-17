select
    line_id,
    invoice_id,
    line_number,
    nullif(trim(cost_center), '') as cost_center,
    gl_account,
    line_amount,
    description,
    loaded_at
from {{ source('raw', 'AP_INVOICE_LINE') }}
