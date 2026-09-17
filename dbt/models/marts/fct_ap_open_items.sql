-- One row per non cancelled AP invoice with what has been paid against it
-- and what remains. Reporting currency is USD at the closing rate on the invoice date.
-- Types are cast explicitly because the mart contract in marts.yml is enforced.
with inv as (
    select * from {{ ref('stg_ap_invoice') }}
    where status <> 'CANCELLED'
),
app as (
    select invoice_id,
           sum(paid_amount) as applied_amount,
           max(payment_date)  as last_applied_date
    from {{ ref('stg_ap_payment') }}
    group by invoice_id
),
fx as (
    select * from {{ ref('stg_fx_rate') }}
),
joined as (
    select
        inv.*,
        coalesce(app.applied_amount, 0)                     as applied_amount,
        inv.gross_amount - coalesce(app.applied_amount, 0)  as outstanding_amount,
        app.last_applied_date,
        fx.rate_to_usd,
        greatest(0, datediff('day', inv.due_date, current_date())) as days_past_due
    from inv
    left join app on app.invoice_id = inv.invoice_id
    left join fx  on fx.from_currency = inv.currency_code and fx.rate_date = inv.invoice_date
)
select
    invoice_id,
    vendor_id,
    entity_code,
    invoice_number,
    invoice_date,
    due_date,
    currency_code,
    status,
    gross_amount::number(18,2)                              as gross_amount,
    applied_amount::number(18,2)                            as paid_amount,
    outstanding_amount::number(18,2)                        as outstanding_amount,
    last_applied_date                                       as last_payment_date,
    rate_to_usd::number(18,8)                               as rate_to_usd,
    round(gross_amount * rate_to_usd, 2)::number(18,2)      as gross_amount_usd,
    round(outstanding_amount * rate_to_usd, 2)::number(18,2) as outstanding_amount_usd,
    days_past_due::number(9,0)                              as days_past_due,
    ({{ aging_bucket('days_past_due') }})::varchar(7)       as aging_bucket,
    loaded_at
from joined
