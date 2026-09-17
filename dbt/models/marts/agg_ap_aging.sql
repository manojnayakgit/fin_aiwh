-- AP aging by legal entity and bucket, USD. The month end pack starts here.
select
    entity_code,
    aging_bucket,
    count(*)                        as invoice_count,
    sum(outstanding_amount_usd)     as outstanding_usd
from {{ ref('fct_ap_open_items') }}
where outstanding_amount > 0
group by 1, 2
