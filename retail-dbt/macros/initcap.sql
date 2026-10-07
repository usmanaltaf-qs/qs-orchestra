{# Title-case a string expression. DuckDB has no initcap(), most warehouses do. #}
{% macro initcap(expr) %}
  {{ return(adapter.dispatch('initcap')(expr)) }}
{% endmacro %}

{% macro default__initcap(expr) %}
  initcap({{ expr }})
{% endmacro %}

{% macro duckdb__initcap(expr) %}
  array_to_string(
    list_transform(string_split(lower({{ expr }}), ' '), w -> upper(w[1]) || w[2:]),
    ' '
  )
{% endmacro %}
