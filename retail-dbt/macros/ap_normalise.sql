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

{# Quantities for display: 44.000 -> 44, 2.500 -> 2.500. #}
{% macro ap_fmt_qty(expr) %}
  case when {{ expr }} = trunc({{ expr }}) then cast(cast({{ expr }} as bigint) as varchar)
       else cast({{ expr }} as varchar) end
{% endmacro %}

{# Read an optional Parquet glob: an empty, typed relation when nothing has been written yet
   (e.g. no explanations or review decisions on the first run). `columns` is "name TYPE, ...". #}
{% macro ap_optional_parquet(path, columns) %}
  {%- set found = 0 -%}
  {%- if execute -%}
    {%- set found = run_query("select count(*) from glob('" ~ path ~ "')").columns[0].values()[0] -%}
  {%- endif -%}
  {%- set names = [] -%}
  {%- for c in columns.split(',') -%}{%- do names.append(c.strip().split(' ')[0]) -%}{%- endfor -%}
  {%- if found > 0 %}
    select {{ names | join(', ') }} from read_parquet('{{ path }}', union_by_name = true)
  {%- else %}
    select {% for c in columns.split(',') -%}
      cast(null as {{ c.strip().split(' ', 1)[1] }}) as {{ c.strip().split(' ')[0] }}{{ ", " if not loop.last }}
    {%- endfor %} where false
  {%- endif %}
{% endmacro %}
