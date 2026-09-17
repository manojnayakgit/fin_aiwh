-- One closing rate per currency per day into USD, the reporting currency.
select
    rate_date,
    upper(from_currency) as from_currency,
    rate::number(18,8)   as rate_to_usd
from {{ source('raw', 'FX_RATE') }}
where upper(to_currency) = 'USD'
  and upper(rate_type) = 'CLOSING'

union all

-- USD to USD is identity, so joins never lose USD invoices.
select distinct rate_date, 'USD', 1.0::number(18,8)
from {{ source('raw', 'FX_RATE') }}
