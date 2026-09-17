-- An invoice with no FX rate silently drops out of every USD total.
select invoice_id, currency_code, invoice_date
from {{ ref('fct_ap_open_items') }}
where rate_to_usd is null
union all
select invoice_id, currency_code, invoice_date
from {{ ref('fct_ar_open_items') }}
where rate_to_usd is null
