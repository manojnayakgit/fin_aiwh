-- AR aging by legal entity and bucket, USD.
select
    entity_code,
    aging_bucket,
    count(*)                        as invoice_count,
    sum(outstanding_amount_usd)     as outstanding_usd
from {{ ref('fct_ar_open_items') }}
where outstanding_amount > 0
group by 1, 2
