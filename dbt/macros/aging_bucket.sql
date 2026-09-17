{#- Standard finance aging buckets from days past due. -#}
{% macro aging_bucket(days_past_due) -%}
    case
        when {{ days_past_due }} <= 0  then 'CURRENT'
        when {{ days_past_due }} <= 30 then '1-30'
        when {{ days_past_due }} <= 60 then '31-60'
        when {{ days_past_due }} <= 90 then '61-90'
        else '90+'
    end
{%- endmacro %}
