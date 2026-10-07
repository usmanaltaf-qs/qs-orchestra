# Schema and distributions

UK retail chain, prices in GBP. All tables are "raw" source-system shaped — no surrogate
keys, no SCDs; dbt does that downstream. Base counts below are at `--scale 1.0`.

## Dimensions (rewritten every run)

### stores — base 25
| column | type | notes |
|---|---|---|
| store_id | INTEGER | 1..n |
| store_name | VARCHAR | `'{city} {suffix}'`, suffix from High Street / Retail Park / Centre |
| city | VARCHAR | from a list of ~20 UK cities |
| region | VARCHAR | derived from city (London, South East, Midlands, North West, Scotland, …) |
| store_type | VARCHAR | flagship 10%, standard 70%, outlet 20% |
| sq_ft | INTEGER | flagship 20k–40k, standard 5k–15k, outlet 3k–8k |
| opened_date | DATE | 2005-01-01 .. 2023-12-31 |

### products — base 500
| column | type | notes |
|---|---|---|
| product_id | INTEGER | 1..n |
| sku | VARCHAR | `'SKU-' \|\| lpad(product_id, 6, '0')` |
| product_name | VARCHAR | `'{brand} {adjective} {subcategory}'` |
| category / subcategory | VARCHAR | Clothing (Tops, Jeans, Jackets), Home (Bedding, Kitchen, Decor), Electronics (Audio, Accessories, Smart Home), Beauty (Skincare, Fragrance), Food (Snacks, Drinks) |
| brand | VARCHAR | ~15 invented brands |
| list_price | DECIMAL(10,2) | per-subcategory range, ending .99 or .49 |
| unit_cost | DECIMAL(10,2) | list_price × 0.35–0.65 |
| is_active | BOOLEAN | 95% true |

### customers — base 10,000
| column | type | notes |
|---|---|---|
| customer_id | INTEGER | 1..n |
| first_name / last_name | VARCHAR | from lists of ~50 each |
| email | VARCHAR | `lower(first.last{id}@example.com)` — ~2% NULL (dirty data) |
| city / region | VARCHAR | same city list as stores |
| signup_date | DATE | spread over the 3 years before the window start |
| loyalty_tier | VARCHAR | none 60%, bronze 25%, silver 10%, gold 5% |
| marketing_opt_in | BOOLEAN | 40% true |

## Facts (partitioned by date)

### orders — base 150/day
Daily volume `n = base × scale × weekday_factor × seasonal_factor × (0.85..1.15 noise)`:
- weekday_factor: Mon–Thu 0.9, Fri 1.1, Sat 1.4, Sun 1.2
- seasonal_factor: Nov ×1.3, Dec ×1.6, Jan ×0.8, else 1.0
Generate with `SELECT day, unnest(range(n)) AS seq FROM days`.

| column | type | notes |
|---|---|---|
| order_id | BIGINT | `yyyymmdd * 1_000_000 + seq` |
| order_date | DATE | partition key |
| order_ts | TIMESTAMP | day + time, weighted toward 10:00–20:00 |
| channel | VARCHAR | store 65%, online 35% |
| store_id | INTEGER | NULL when online |
| customer_id | INTEGER | skewed (`floor(n_customers × u²) + 1`) so some customers are frequent; NULL for ~15% of online (guest) and ~40% of store (no loyalty card) |
| status | VARCHAR | completed 92%, cancelled 5%, pending 3% (pending only within 2 days of window end) |

### order_items — avg ~2.3 lines/order
Lines per order 1–5 (weights 40/30/15/10/5). Product chosen with popularity skew.

| column | type | notes |
|---|---|---|
| order_item_id | BIGINT | `order_id * 10 + line_number` |
| order_id | BIGINT | |
| order_date | DATE | partition key (denormalised on purpose) |
| line_number | INTEGER | 1..5 |
| product_id | INTEGER | |
| quantity | INTEGER | 1 (75%), 2 (18%), 3–5 (7%) |
| unit_price | DECIMAL(10,2) | product list_price |
| discount_pct | DECIMAL(4,2) | 0 (70%), 0.10 (15%), 0.20 (10%), 0.50 (5%; higher in Jan) |
| line_total | DECIMAL(12,2) | `round(quantity × unit_price × (1 − discount_pct), 2)` |

### returns — ~6% of completed order lines
| column | type | notes |
|---|---|---|
| return_id | BIGINT | `order_item_id` (1 return max per line) |
| order_item_id | BIGINT | |
| return_date | DATE | order_date + 1..30 days; partition key |
| reason | VARCHAR | wrong size, damaged, not as described, changed mind, late delivery (online only) |
| refund_amount | DECIMAL(12,2) | line_total, or 50% of it for ~10% "partial refund" |

Electronics and Clothing return at ~2× the base rate; Food almost never.

### inventory_snapshots — weekly (Sundays) by default
Written only when a Sunday falls in the window. `--inventory-frequency daily` switches to daily.

| column | type | notes |
|---|---|---|
| snapshot_date | DATE | partition key |
| store_id | INTEGER | physical stores only |
| product_id | INTEGER | active products only |
| on_hand_qty | INTEGER | 0–200, ~5% exact zero (stockouts) |
| on_order_qty | INTEGER | 0 normally, 20–100 when on_hand < 20 |

At scale 1: 25 × ~475 × 52 ≈ 620k rows/year. Warn in logs if a run would exceed 50M rows.

## Deliberate messiness (keep it — it makes downstream tests meaningful)
- NULL emails (~2%), guest orders, NULL store_id for online
- a few customers with whitespace/casing issues in `city` (~1%)
- pending orders near the window end

Make each of these switchable off with `--clean`.

## Adding a table — checklist
1. Pick its grain and a deterministic key (date-keyed if it's a fact).
2. Generate in SQL using `u(key, '<column>')` for every random column — new salt per column.
3. If it's date-based, include its partition column and fit it into the `[start-30d, end]`
   window logic.
4. Add it to both writers (duckdb + parquet) and to the run summary.
5. Add PK/FK/non-empty checks to `validate_retail.py`.
6. Update this file.
