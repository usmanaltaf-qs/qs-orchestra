# Dashboard specs

Metric definitions: see `retail-dbt-analytics/references/models.md`. Don't redefine them here.

## Metrics to implement
| metric | on model | type |
|---|---|---|
| net_sales, gross_sales, discount_amount, cogs, units | fct_order_lines | sum |
| gross_margin, gross_margin_pct, discount_rate | fct_order_lines | number (custom SQL) |
| orders, customers (distinct, non-null) | fct_orders | count_distinct |
| aov, cancellation_rate | fct_orders | number |
| refunds, returns_count | fct_returns | sum / count |
| return_rate | rpt_daily_sales or rpt_kpi_daily | number (needs refunds and net_sales on one model) |
| stockout_rate, avg_weeks_of_cover | fct_inventory_snapshots / rpt_inventory_health | number / average |
| retention_pct | rpt_customer_cohorts | average |

## 1. Exec overview (build first)
Filters: date range (default last 90 days), channel, region.
- Big numbers: net sales, orders, AOV, gross margin %, return rate, each vs previous period
- Line: net sales by week, this year vs last year
- Bar: net sales by channel by month
- Bar: net sales by category
- Table: top 10 stores by net sales with margin % and return rate

## 2. Product & category
Filters: date range, category, brand.
- Treemap/bar: net sales by category → subcategory
- Scatter: products by units sold vs gross margin %
- Table: top/bottom 20 products by net sales, with discount rate and return rate
- Line: discount rate by week (Jan sale should be visible)

## 3. Stores
Filters: date range, region, store_type.
- Bar: net sales per sq ft by store
- Heatmap or table: weekday × store net sales (weekend uplift should be visible)
- Line: store vs online share of net sales over time

## 4. Customers & retention
Filters: cohort month range, loyalty tier.
- Cohort retention triangle (rpt_customer_cohorts) as a pivot table, conditional formatting
- Big numbers: active customers, new customers this month, % of sales from guests
- Bar: net sales by value_segment and loyalty_tier

## 5. Inventory
Filters: region, category.
- Big numbers: stockout rate (latest snapshot), avg weeks of cover
- Table: products stocked out in ≥3 stores, with trailing 28d units sold
- Bar: weeks of cover by category (flag < 1 and > 12)

## Sanity checks the generator should make visible
If these don't show up, either the generator or the models are wrong. Flag it, don't hide it:
Dec peak and Jan dip, Saturday highest weekday, Electronics/Clothing highest return
rates, Food near-zero returns, ~35% online share.
