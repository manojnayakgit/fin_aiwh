-- Working capital KPIs per entity, as of today.
-- DSO = AR outstanding / (AR invoiced in trailing 90 days / 90)
-- DPO = AP outstanding / (AP invoiced in trailing 90 days / 90)
-- Count back method, the common management reporting convention. Not GAAP.
with ar as (
    select
        entity_code,
        sum(outstanding_amount_usd) as ar_outstanding_usd,
        sum(case when invoice_date >= dateadd('day', -90, current_date())
                 then gross_amount_usd else 0 end) as ar_invoiced_90d_usd
    from {{ ref('fct_ar_open_items') }}
    group by 1
),
ap as (
    select
        entity_code,
        sum(outstanding_amount_usd) as ap_outstanding_usd,
        sum(case when invoice_date >= dateadd('day', -90, current_date())
                 then gross_amount_usd else 0 end) as ap_invoiced_90d_usd
    from {{ ref('fct_ap_open_items') }}
    group by 1
)
select
    coalesce(ar.entity_code, ap.entity_code)              as entity_code,
    current_date()                                        as as_of_date,
    ar.ar_outstanding_usd,
    ap.ap_outstanding_usd,
    round(ar.ar_outstanding_usd / nullif(ar.ar_invoiced_90d_usd / 90, 0), 1) as dso_days,
    round(ap.ap_outstanding_usd / nullif(ap.ap_invoiced_90d_usd / 90, 0), 1) as dpo_days
from ar
full outer join ap on ap.entity_code = ar.entity_code
