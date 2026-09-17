-- Two closing rates for the same currency on the same day would double count
-- every invoice in that currency after the join. Fails if any exist.
select rate_date, from_currency, count(*) as n
from {{ ref('stg_fx_rate') }}
group by 1, 2
having count(*) > 1
