{# Normalisers for matching invoice values to master data. #}

{# IDs (invoice/PO/VAT numbers, SKUs): trim, drop all whitespace, upper-case. Empty -> NULL. #}
{% macro ap_norm_id(expr) %}
  nullif(upper(regexp_replace(trim({{ expr }}), '\s+', '', 'g')), '')
{% endmacro %}

{# Bank sort codes and account numbers: digits only. Empty -> NULL. #}
{% macro ap_digits(expr) %}
  nullif(regexp_replace({{ expr }}, '[^0-9]', '', 'g'), '')
{% endmacro %}

{# Company names: lower-case, & -> and, punctuation out, legal suffixes out, single spaces.
   "Bramley & Hart Trading Ltd." and "bramley and hart trading" compare equal. #}
{% macro ap_norm_name(expr) %}
  nullif(trim(regexp_replace(
    regexp_replace(
      regexp_replace(replace(lower({{ expr }}), '&', ' and '), '[^a-z0-9 ]', ' ', 'g'),
      '\b(ltd|limited|plc|llp|co|company)\b', ' ', 'g'),
    '\s+', ' ', 'g')), '')
{% endmacro %}

{# Free-text descriptions for similarity matching: lower-case, single spaces. #}
{% macro ap_norm_text(expr) %}
  nullif(trim(regexp_replace(lower({{ expr }}), '\s+', ' ', 'g')), '')
{% endmacro %}
