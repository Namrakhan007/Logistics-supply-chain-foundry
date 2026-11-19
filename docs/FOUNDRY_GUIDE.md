# Foundry Build Guide — Logistics Supply Chain Challenge

**Author:** Namra Khan
**Platform:** Palantir Foundry (Pipeline Builder, Ontology Manager, Code Repositories, Workshop)

This guide shows how I built the project end to end, in the order I built it. It is written for people who already know Foundry, so it lists what I created and how I configured it, not how each tool works. The reasoning behind each decision is in the report.

---

## 0. Overview

| Layer | Tool | What I built |
|---|---|---|
| 1 · Pipeline | Pipeline Builder | `logistics_pipeline`: 3 raw files → 7 clean, integrated `_Final` datasets |
| 2 · Ontology | Ontology Manager | 10 object types (prefix `LSC`), 7 link types |
| 3 · Logic & actions | Code Repositories, Actions, Schedules | 2 Python transforms, Review and Reopen actions, 1 event-driven schedule |
| 4 · Application | Workshop | `Logistics Control Tower`, 4 tabs |

**Naming conventions**

- Object types are prefixed **`LSC`** (Logistics Supply Chain), so they are easy to find in a shared ontology.
- Published datasets end in **`_Final`**: stable outputs that the ontology and the code read from.
- Everything lives in one project: **Logistics Supply Chain Challenge**.

**Build order:** pipeline → ontology → restock transform → recommendation object → actions → schedule → insights transform → insight objects → Workshop.

---

## 1. Project setup and ingestion

1. Create the project **Logistics Supply Chain Challenge**.
2. Upload `Orders.csv`, `Shipments.csv` and `Inventory.csv` as **structured datasets** (Orders, Shipments, Inventory).
3. Check the row counts: **100 / 80 / 57**. The raw datasets are never edited.

---

## 2. Layer 1 — Pipeline Builder (`logistics_pipeline`)

Create a batch pipeline and add the three raw datasets.

### 2.1 Clean nodes (one per source)

| Node | Transforms, in order | De-duplicate on |
|---|---|---|
| `Clean_Orders` | Trim `order_id`, `customer_id`, `product_id`, `order_status` → Uppercase the 3 ID columns → Cast `order_date` to **Date** → Filter rows where the IDs are not null (empty string treated as null) | `order_id` |
| `Clean_Shipments` | Trim all 4 columns → Uppercase `shipment_id`, `order_id` → Filter on non-null keys | `shipment_id` |
| `Clean_Inventory` | Trim → Uppercase `product_id`, `warehouse_id` → Filter on non-null keys | `product_id` + `warehouse_id` |

Status columns are trimmed but **not** uppercased, because the next step compares exact values such as `Delivered` and `Canceled`.

### 2.2 Reconcile orders and shipments

1. **Join** `Orders_Shipments_Join`: **left join**, `Clean_Orders` on the left, `order_id = order_id`. Keep `shipment_id`, `carrier`, `status` from the right side.
2. **Rename** `status` → `shipment_status`.
3. **Case** → new column `data_quality_flag` (first match wins). This node is `Orders_Checked`.

| # | Condition | Value |
|---|---|---|
| 1 | `order_status = Canceled` AND `shipment_id` is not null | `CANCELED_BUT_SHIPPED` |
| 2 | `order_status = Pending` AND `shipment_id` is not null | `PENDING_BUT_SHIPPED` |
| 3 | `order_status = Delivered` AND `shipment_id` is null | `DELIVERED_NO_SHIPMENT` |
| 4 | `order_status = Delivered` AND `shipment_status = In Transit` | `STATUS_MISMATCH` |
| 5 | `order_status = Delivered` AND `shipment_status = Delayed` | `STATUS_MISMATCH` |
| 6 | `order_status = Shipped` AND `shipment_id` is null | `SHIPPED_NO_SHIPMENT` |
| else | — | `OK` |

**Check:** OK 55 · DELIVERED_NO_SHIPMENT 12 · STATUS_MISMATCH 11 · CANCELED_BUT_SHIPPED 10 · PENDING_BUT_SHIPPED 8 · SHIPPED_NO_SHIPMENT 4 (**45 conflicts**).

### 2.3 Derived entities (Aggregate transforms)

| Node | Source | Group by | Aggregations | Rows |
|---|---|---|---|---|
| `Customers` | Clean_Orders | `customer_id` | count → `total_orders`, min/max `order_date` → `first_order_date`, `last_order_date` | 28 |
| `Products` | Clean_Inventory | `product_id` | sum `stock_quantity` → `total_stock`, count → `warehouse_count` | 19 |
| `Warehouses` | Clean_Inventory | `warehouse_id` | sum `stock_quantity` → `total_stock`, count → `product_count` | 3 |
| `Inventory_Final` | Clean_Inventory | — | Concatenate `product_id` + `-` + `warehouse_id` → `inventory_key` (null output if any input is null) | 57 |

### 2.4 Carriers (lead time without shipment dates)

From `Clean_Shipments`:

1. Case: `status = Delayed` → 1, else 0 → `is_delayed`
2. Aggregate by `carrier`: count → `total_shipments`, sum `is_delayed` → `delayed_shipments`
3. Divide → `delay_rate`
4. Constant `5` → `standard_lead_time_days`
5. `standard × (1 + delay_rate)` → `expected_lead_time_days`, rounded to 2 decimals

**Check:** CarrierA 5.61 · CarrierB 5.58 · CarrierC 5.24.

### 2.5 Outputs and deploy

Add 7 dataset outputs: `Orders_Final` (from Orders_Checked), `Customers_Final`, `Products_Final`, `Warehouses_Final`, `Inventory_Final`, `Shipments_Final` (from Clean_Shipments), `Carriers_Final`.

Deploy → **7/7 outputs built**.

---

## 3. Layer 2 — Ontology Manager

### 3.1 Object types (from the `_Final` datasets)

| Object type | Backing dataset | Primary key | Title | Objects |
|---|---|---|---|---|
| LSC Order | Orders_Final | order_id | order_id | 100 |
| LSC Customer | Customers_Final | customer_id | customer_id | 28 |
| LSC Product | Products_Final | product_id | product_id | 19 |
| LSC Warehouse | Warehouses_Final | warehouse_id | warehouse_id | 3 |
| LSC Inventory | Inventory_Final | inventory_key | inventory_key | 57 |
| LSC Shipment | Shipments_Final | shipment_id | shipment_id | 80 |
| LSC Carrier | Carriers_Final | carrier | carrier | 3 |

- Skip the auto-generated create, modify and delete actions. All edits go through the two actions in step 6.
- Add a **description to every property**. The full data dictionary is in the report, section 1.11.
- Save, wait for indexing, and confirm each count matches the table above.

### 3.2 Link types (foreign-key links)

| Link | Foreign key (many side) | Primary key | Cardinality |
|---|---|---|---|
| Customer ↔ Order | LSC Order.customer_id | LSC Customer.customer_id | 1 : many |
| Product ↔ Order | LSC Order.product_id | LSC Product.product_id | 1 : many |
| Order ↔ Shipment | LSC Shipment.order_id | LSC Order.order_id | 1 : 0..1 |
| Carrier ↔ Shipment | LSC Shipment.carrier | LSC Carrier.carrier | 1 : many |
| Product ↔ Inventory | LSC Inventory.product_id | LSC Product.product_id | 1 : many |
| Warehouse ↔ Inventory | LSC Inventory.warehouse_id | LSC Warehouse.warehouse_id | 1 : many |
| Inventory ↔ Restock Recommendation | LSC Restock Recommendation.inventory_key | LSC Inventory.inventory_key | 1 : many (added in step 5) |

Order ↔ Shipment uses `Shipment.order_id` because all 80 shipments have an order ID, while 20 orders have no shipment.

**Check in Object Explorer:** customer C007 → 5 linked orders.

---

## 4. Layer 3 — Code Repository (`logistics_restock_automation`)

Create a **Python transforms** repository in the project. Both transforms go in `transforms-python/src/myproject/datasets/`. `pipeline.py` discovers them automatically, so it needs no changes.

```
logistics_restock_automation/
└── transforms-python/src/myproject/
    ├── pipeline.py                      # discover_transforms(datasets)
    └── datasets/
        ├── restock_recommendations.py   # Task 2 restock engine
        └── supply_chain_insights.py     # Task 3 insight tables
```

### 4.1 `restock_recommendations.py` (Task 2)

| | |
|---|---|
| Inputs | `Orders_Final`, `Inventory_Final`, `Carriers_Final` |
| Output | `Restock_Recommendations` (22 columns) |
| Logic | 30-day non-canceled demand → Poisson 95% reorder point using the shipment-weighted carrier lead time (5.5 days) → policy floor of 20 → max-heap transfer optimiser |
| Settings | the `PARAMS` dictionary at the top of the file |

The business logic is plain pandas, and the `@transform` wrapper at the bottom handles Spark I/O. **Gotcha:** the ontology rejects NaN, so the wrapper writes `days_of_cover` as null for products with no sales.

Commit → wait for Checks → Build. **Check:** 11 rows, 120 units, `purchase_quantity` = 0 on every row.

### 4.2 `supply_chain_insights.py` (Task 3)

| | |
|---|---|
| Inputs | `Orders_Final`, `Shipments_Final`, `Carriers_Final`, `Inventory_Final`, `Restock_Recommendations` |
| Outputs | `Carrier_Delivery_Performance` (3 rows), `Top_Products_Last_Quarter` (5 rows), `Warehouse_Shortage` (3 rows), all from one build |
| Rules | Last quarter = last complete calendar quarter (Q1 2025). Top 5 ties broken by distinct customers, then product ID. Demand trend = short items' sales in the last 30 days vs the previous 30 days. |

The same NaN-to-null fix applies to `demand_trend_pct`.

**Check:** CarrierC 5.24 / CarrierB 5.58 / CarrierA 5.61 · Top 5 = P012, P001, P003, P007, P009 · Shortage W03 51 / W02 44 / W01 25, all Falling.

---

## 5. Ontology for the code outputs

| Object type | Backing dataset | Primary key | Title | Objects | Notes |
|---|---|---|---|---|---|
| LSC Restock Recommendation | Restock_Recommendations | recommendation_id | inventory_key | 11 | **Edits enabled**, so decisions survive rebuilds. Link to LSC Inventory on `inventory_key`. |
| LSC Top Product | Top_Products_Last_Quarter | product_id | product_id | 5 | Read-only, no actions |
| LSC Warehouse Shortage | Warehouse_Shortage | warehouse_id | warehouse_id | 3 | Read-only, no actions |

---

## 6. Actions (on LSC Restock Recommendation)

| | Review Restock Recommendation | Reopen recommendation |
|---|---|---|
| Type | Modify object | Modify object |
| Parameters | `Decision` (required: Approved / Rejected) | `Reopen reason` (required string, min length 10) |
| Rule | `Status` = Decision | `Status` = static value `Pending Review` |
| Submission criteria | user in organisation **AND** `Status` **is** `Pending Review` | user in organisation **AND** `Status` **is not** `Pending Review` |
| Failure message | "This recommendation has already been reviewed." | "This recommendation is still pending. Use Review instead." |

Together the two guards make every status change a deliberate step: Review can't decide an item twice, and Reopen can't reopen an item that's still pending.

---

## 7. Schedule (on Restock_Recommendations)

| Setting | Value |
|---|---|
| Name | Daily restock recommendations |
| Target | Restock_Recommendations only |
| Trigger | when **any** of `Orders_Final`, `Inventory_Final`, `Carriers_Final` updates |

It rebuilds only when an input changes, and a manual run with no changes reports "all datasets up-to-date".

---

## 8. Layer 4 — Workshop (`Logistics Control Tower`)

One module, with the header and a tab bar of four tabs.

| Tab | Widgets | Data |
|---|---|---|
| **Overview** | 4 metric cards: Pending review (count where Status = Pending Review), Critical stock-outs (count where priority = CRITICAL), Units to transfer (sum of transfer quantity), Order conflicts (count where flag ≠ OK). Markdown "How to use this dashboard". | LSC Restock Recommendation, LSC Order |
| **Restock Operations** | Object table of LSC Restock Recommendation, sorted by priority then stock. Button group: **Review selected recommendation** (primary) and **Reopen** (minimal). Each button's object parameter defaults to *Object table → Active object* and is disabled in the form. | LSC Restock Recommendation |
| **Supply Chain Insights** | Left: average delivery time per carrier (bar), Top 5 products Q1 2025 (bar, sum of `units_sold`, sorted descending) plus a note on the tie rule. Right: shortage per warehouse (stacked by trigger type), demand of short items, prev vs last 30 days (grouped bars), plus an insight line. Left/right columns at flex 2 : 3. | LSC Carrier, LSC Top Product, LSC Restock Recommendation, LSC Warehouse Shortage |
| **Data Quality** | Bar chart of flagged orders by conflict type, plus an object table of the flagged orders | LSC Order filtered to flag ≠ OK |

Save and publish, then test in **View** mode: approve one item (Pending drops by 1), then reopen it with a reason (Pending goes back up).

---

## 9. End-to-end verification

| Stage | Expected |
|---|---|
| Raw rows | 100 / 80 / 57 |
| Data-quality flags | 45 conflicts, 55 OK |
| Object counts | 100 · 28 · 19 · 3 · 57 · 80 · 3 · 11 · 5 · 3 |
| Restock output | 11 recommendations, 120 units, 0 purchased |
| Carrier lead time | 5.61 / 5.58 / 5.24 days |
| Top 5 (Q1 2025) | P012, P001, P003, P007, P009 |
| Shortage | W03 51 · W02 44 · W01 25 |

---

## 10. Issues I hit and how I fixed them

| Issue | Fix |
|---|---|
| Case rule compared `shipment_id` instead of `shipment_status`; one rule used *not null* instead of *null*. No error, just wrong counts. | Checked every flag count against an independent count after each change |
| Ontology indexing failed with `DataFormatValidationFailures` | NaN written as null in both transforms |
| Schedule waited for all inputs | Changed the trigger to **any** input |
| Action parameter defaulted to the whole object set | Default set to *Object table → Active object* |
| Wizard-created Reopen action allowed setting any status | Removed the free Status parameter, set a static value, and added a reason and submission criteria |
| Top 5 had a four-way tie at 5 units | Moved the ranking into `supply_chain_insights.py` with an explicit tie rule |
