# Model specs

## Business definitions (single source of truth — use these everywhere)

| term | definition |
|---|---|
| gross_sales | `quantity * unit_price` on lines of **completed** orders |
| discount_amount | `gross_sales - line_total` |
| net_sales | `sum(line_total)` on completed orders, by order_date |
| refunds | `sum(refund_amount)` by **return_date** (not order date) |
| net_sales_after_returns | net_sales − refunds, aligned on the reporting date |
| cogs | `quantity * unit_cost` (cost from products) |
| gross_margin | net_sales − cogs; margin % = gross_margin / net_sales |
| AOV | net_sales / completed orders |
| return_rate | refunds / net_sales over the same period (value-based) |
| active customer | ≥1 completed order in the trailing 90 days |
| stockout | `on_hand_qty = 0` on a snapshot |

Cancelled and pending orders are excluded from sales but kept in `fct_orders` with a
status, so cancellation rate can be reported.

## Staging (views)
`stg_retail__customers`, `__products`, `__stores`, `__orders`, `__order_items`,
`__returns`, `__inventory_snapshots` — 1:1 with sources, cleaned and typed.

## Intermediate
- `int_order_lines_enriched` — order_items ⨝ orders ⨝ products: adds status, channel,
  store_id, customer_id, unit_cost, category, gross_sales, discount_amount, cogs.
- `int_customer_orders` — per customer: first/last order date, order count, lifetime net sales.

## Core marts (tables)
| model | grain | notes |
|---|---|---|
| `dim_date` | day | from `dbt_utils.date_spine` or a generated series; year, quarter, month, iso week, weekday, is_weekend, retail season (Black Friday week, Christmas, January sale) |
| `dim_customers` | customer | + first_order_date, cohort_month, lifetime orders/net sales, days_since_last_order, is_active, value_segment (top 10% / next 40% / rest by lifetime net sales) |
| `dim_products` | product | + price_band, margin_pct |
| `dim_stores` | store | + 'Online' row (store_id = 0) so online sales join cleanly; facts coalesce NULL store_id → 0 |
| `fct_orders` | order | status, channel, store, customer (nullable = guest), item_count, units, gross/net sales, discount, cogs, has_return |
| `fct_order_lines` | order line | **incremental**; all line measures + is_returned, refund_amount |
| `fct_returns` | return | + days_to_return, category, channel |
| `fct_inventory_snapshots` | date × store × product | + is_stockout, trailing_28d_units_sold, weeks_of_cover (NULL when no sales) |

## Reporting marts (tables, small)
| model | grain | for dashboard |
|---|---|---|
| `rpt_daily_sales` | date × store × category × channel | Exec, Stores, Product |
| `rpt_customer_cohorts` | cohort_month × months_since_first_order | Customers (retention triangle) |
| `rpt_inventory_health` | latest snapshot × store × category | Inventory |
| `rpt_kpi_daily` | date | Exec headline numbers incl. WoW and YoY deltas |

Lightdash can aggregate the core facts itself, so only add a `rpt_` model when it
pre-computes something awkward in a semantic layer (cohorts, period-over-period, snapshots).

## Implementation notes (decisions made while building)
- Line sales measures (`gross_sales`, `net_sales`, `discount_amount`, `cogs`) are 0 on
  non-completed lines, so facts can be summed without a status filter. `line_total` and
  `fct_orders.order_value` keep the raw value.
- Returns of orders before the data window (generator's 30-day lookback) stay in
  `fct_returns` with `has_order_line = false`; refunds include them, attributes are NULL.
- `fct_order_lines` incremental filter = last N days of `order_date` OR `order_item_id`
  returned in the last N days (N = var `incremental_lookback_days`, default 3).
- `dim_customers` measures recency/`is_active` against the latest order date in the data,
  not today. `value_segment` adds 'No orders' for customers who never bought.
- `rpt_daily_sales` has no order counts (non-additive across categories).

## Accounts payable (`models/ap/`, tag `ap`)
Owned by the `invoice-processing` skill (checks and status rules: its `references/matching.md`).
Sources read `INVOICES_SOURCE_URI` (system/ + extracted/); `INVOICES_EXTRACTED_URI` can point
extracted/ elsewhere (the matching eval feeds ground truth in that way).

| model | grain | notes |
|---|---|---|
| `stg_ap__inbox_files` | inbox file path | `invoice_id` = md5(path): a byte-identical re-send is its own invoice |
| `stg_ap__extractions` / `_extraction_lines` | file hash (latest run) | values as printed; IDs normalised by `ap_norm_*` macros |
| `int_ap__invoices` | invoice | inbox file ⨝ extraction of its content |
| `int_ap__invoice_checks` | invoice × check | `passed` NULL = not applicable; thresholds are `ap_*` vars |
| `fct_invoice_status` | invoice | duplicate > needs_review > exception > auto_approved; `check_hash` keys explanations |
| `rpt_ap_daily` | date × final_status × exception_type | an invoice appears once per exception type |

- Line checks (price, quantity, unmatched line, VAT rate) only run against a PO that exists and
  belongs to the matched supplier; otherwise they're NULL and the PO check holds the invoice.
- Build with `dbt build --select +tag:ap --indirect-selection cautious` so retail tests whose
  models aren't selected don't get pulled in. CI's full build excludes `tag:ap`.
