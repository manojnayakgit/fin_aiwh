-- shield test: STATUS is contracted NOT NULL but upstream relaxed it, see https://github.com/manojnayakgit/fin_aiwh/issues/4
-- Fails the build the moment a null arrives, so it cannot flow into a report unseen.
select status
from {{ ref('stg_ar_invoice') }}
where status is null
