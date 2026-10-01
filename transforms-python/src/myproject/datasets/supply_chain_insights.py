"""
Supply chain insights (Task 3).

One Foundry Python transform that turns the cleaned datasets and the restock
recommendations into three small, chart-ready insight tables:

1. Carrier_Delivery_Performance  - average delivery time per carrier
2. Top_Products_Last_Quarter     - top 5 best sellers in the last full quarter
3. Warehouse_Shortage            - shortage per warehouse, with demand trend

All business logic lives in plain pandas functions so it can be tested locally
and explained line by line; the Foundry wrapper at the bottom only reads and
writes datasets.
"""

import re

import pandas as pd
from pyspark.sql import functions as F
from transforms.api import transform, Input, Output

TOP_N = 5
TREND_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# 1. Average delivery time per carrier
# ---------------------------------------------------------------------------
def carrier_delivery_performance(shipments: pd.DataFrame,
                                 carriers: pd.DataFrame) -> pd.DataFrame:
    """Status mix per carrier plus the average delivery time.

    Shipments carry no ship/delivery timestamps, so delivery time cannot be
    measured directly. The average used here is the carrier's expected lead
    time: a 5-day standard adjusted by the carrier's MEASURED delay rate
    (built in Layer 1, editable on the LSC Carrier object). The method column
    states this explicitly so the number is never mistaken for a measurement.
    """
    counts = (shipments.assign(status=shipments["status"].str.strip())
              .pivot_table(index="carrier", columns="status",
                           values="shipment_id", aggfunc="count", fill_value=0)
              .reset_index())
    for col in ["Delivered", "In Transit", "Delayed"]:
        if col not in counts:
            counts[col] = 0

    df = carriers.merge(counts, on="carrier", how="left")
    df["delivered_shipments"] = df["Delivered"].astype(int)
    df["in_transit_shipments"] = df["In Transit"].astype(int)
    df["on_time_rate"] = (1 - df["delay_rate"]).round(3)
    df["avg_delivery_days"] = df["expected_lead_time_days"].round(2)
    df["method"] = "Estimated: 5-day standard x (1 + measured delay rate)"
    df = df.sort_values("avg_delivery_days").reset_index(drop=True)
    df["speed_rank"] = range(1, len(df) + 1)

    return df[["speed_rank", "carrier", "avg_delivery_days", "total_shipments",
               "delivered_shipments", "in_transit_shipments", "delayed_shipments",
               "delay_rate", "on_time_rate", "method"]]


# ---------------------------------------------------------------------------
# 2. Top 5 best sellers in the last full quarter
# ---------------------------------------------------------------------------
def last_full_quarter(reference_date: pd.Timestamp):
    """Start and end of the last complete calendar quarter before the reference date."""
    current_q_start = reference_date.to_period("Q").start_time
    prev_q = (current_q_start - pd.Timedelta(days=1)).to_period("Q")
    return prev_q.start_time, prev_q.end_time.normalize(), str(prev_q)  # e.g. 2025Q1


def top_products_last_quarter(orders: pd.DataFrame, top_n: int = TOP_N) -> pd.DataFrame:
    """Rank products by units sold in the last full quarter.

    Rules
    - Canceled orders are excluded (they are not sales); 1 order = 1 unit.
    - Ties on units are broken by the number of DISTINCT customers who bought
      the product: demand spread across many customers is more reliable than
      repeat orders from a few. Product ID is a final, deterministic tiebreak.
    """
    o = orders.copy()
    o["order_date"] = pd.to_datetime(o["order_date"])
    ref = o["order_date"].max().normalize()
    start, end, label = last_full_quarter(ref)

    q = o[(o["order_date"] >= start) & (o["order_date"] <= end)
          & (o["order_status"] != "Canceled")]

    ranked = (q.groupby("product_id")
               .agg(units_sold=("order_id", "size"),
                    distinct_customers=("customer_id", "nunique"))
               .reset_index()
               .sort_values(["units_sold", "distinct_customers", "product_id"],
                            ascending=[False, False, True])
               .reset_index(drop=True))
    ranked["sales_rank"] = range(1, len(ranked) + 1)
    ranked["share_of_quarter_sales"] = (ranked["units_sold"] / len(q)).round(3)
    ranked["quarter"] = label.replace("Q", "-Q")
    ranked["period_start"] = start.date()
    ranked["period_end"] = end.date()

    return ranked.head(top_n)[["sales_rank", "product_id", "units_sold",
                               "distinct_customers", "share_of_quarter_sales",
                               "quarter", "period_start", "period_end"]]


# ---------------------------------------------------------------------------
# 3. Shortage per warehouse, based on demand
# ---------------------------------------------------------------------------
def _product_demand(orders: pd.DataFrame, start: pd.Timestamp,
                    end: pd.Timestamp) -> pd.Series:
    """Units sold per product in [start, end]; canceled orders excluded."""
    w = orders[(orders["order_date"] >= start) & (orders["order_date"] <= end)
               & (orders["order_status"] != "Canceled")]
    return w.groupby("product_id").size()


def warehouse_shortage(recommendations: pd.DataFrame, orders: pd.DataFrame,
                       inventory: pd.DataFrame,
                       window: int = TREND_WINDOW_DAYS) -> pd.DataFrame:
    """Shortage per warehouse = units needed to reach the demand-based target stock.

    The target stock comes from the restock engine (last 30 days of sales,
    carrier lead time, 95% service level, policy floor), so the shortage is
    driven by demand, not by a fixed stock threshold.

    Demand trend: for the products that are short in each warehouse, compare
    units sold in the last 30 days with the previous 30 days. This shows
    whether a shortage is caused by rising demand or by how stock is placed.
    """
    o = orders.copy()
    o["order_date"] = pd.to_datetime(o["order_date"])
    ref = o["order_date"].max().normalize()
    last = _product_demand(o, ref - pd.Timedelta(days=window - 1), ref)
    prev = _product_demand(o, ref - pd.Timedelta(days=2 * window - 1),
                           ref - pd.Timedelta(days=window))

    r = recommendations.copy()
    r["demand_last"] = r["product_id"].map(last).fillna(0)
    r["demand_prev"] = r["product_id"].map(prev).fillna(0)

    # Units each warehouse will SEND to others, parsed from "W02 (20), W01 (5)".
    sent = {}
    for text in r["transfer_from"].dropna():
        for wh, qty in re.findall(r"(W\d+) \((\d+)\)", text):
            sent[wh] = sent.get(wh, 0) + int(qty)

    def units(priority):
        return r[r["priority"] == priority].groupby("warehouse_id")[
            "recommended_restock_quantity"].sum()

    g = r.groupby("warehouse_id")
    df = pd.DataFrame({"warehouse_id": sorted(inventory["warehouse_id"].unique())})
    wh = df["warehouse_id"]
    df["items_below_target"] = wh.map(g.size()).fillna(0).astype(int)
    df["shortage_units"] = wh.map(g["recommended_restock_quantity"].sum()).fillna(0).astype(int)
    df["critical_units"] = wh.map(units("CRITICAL")).fillna(0).astype(int)
    df["high_units"] = wh.map(units("HIGH")).fillna(0).astype(int)
    df["medium_units"] = wh.map(units("MEDIUM")).fillna(0).astype(int)
    df["units_received_by_transfer"] = wh.map(g["transfer_quantity"].sum()).fillna(0).astype(int)
    df["units_to_purchase"] = wh.map(g["purchase_quantity"].sum()).fillna(0).astype(int)
    df["units_sent_by_transfer"] = wh.map(sent).fillna(0).astype(int)
    df["net_transfer_units"] = df["units_received_by_transfer"] - df["units_sent_by_transfer"]
    df["stock_on_hand"] = wh.map(
        inventory.groupby("warehouse_id")["stock_quantity"].sum()).astype(int)
    df["short_items_demand_last_30d"] = wh.map(g["demand_last"].sum()).fillna(0).astype(int)
    df["short_items_demand_prev_30d"] = wh.map(g["demand_prev"].sum()).fillna(0).astype(int)
    prev_d = df["short_items_demand_prev_30d"].where(df["short_items_demand_prev_30d"] > 0)
    df["demand_trend_pct"] = ((df["short_items_demand_last_30d"] - prev_d) / prev_d * 100).round(1)
    df["demand_trend"] = (pd.cut(df["demand_trend_pct"], [-1e9, -10, 10, 1e9],
                                 labels=["Falling", "Stable", "Rising"])
                          .astype(str).replace("nan", "No prior sales"))
    df["reference_date"] = ref.date()
    return df.sort_values("shortage_units", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Foundry transform
# ---------------------------------------------------------------------------
PROJECT = "/Aplos Data-5a9492/Logistics Supply Chain Challenge"


@transform(
    carrier_out=Output(f"{PROJECT}/Carrier_Delivery_Performance"),
    top_out=Output(f"{PROJECT}/Top_Products_Last_Quarter"),
    shortage_out=Output(f"{PROJECT}/Warehouse_Shortage"),
    orders=Input(f"{PROJECT}/Orders_Final"),
    shipments=Input(f"{PROJECT}/Shipments_Final"),
    carriers=Input(f"{PROJECT}/Carriers_Final"),
    inventory=Input(f"{PROJECT}/Inventory_Final"),
    recommendations=Input(f"{PROJECT}/Restock_Recommendations"),
)
def compute(ctx, carrier_out, top_out, shortage_out,
            orders, shipments, carriers, inventory, recommendations):
    o = orders.dataframe().toPandas()
    inv = inventory.dataframe().toPandas()
    spark = ctx.spark_session

    carrier_out.write_dataframe(spark.createDataFrame(
        carrier_delivery_performance(shipments.dataframe().toPandas(),
                                     carriers.dataframe().toPandas())))
    top_out.write_dataframe(spark.createDataFrame(top_products_last_quarter(o)))
    shortage = spark.createDataFrame(
        warehouse_shortage(recommendations.dataframe().toPandas(), o, inv))
    # The ontology rejects NaN: store "no previous sales" as an empty (null) trend.
    shortage = shortage.withColumn(
        "demand_trend_pct",
        F.when(F.isnan("demand_trend_pct"), F.lit(None)).otherwise(F.col("demand_trend_pct")))
    shortage_out.write_dataframe(shortage)