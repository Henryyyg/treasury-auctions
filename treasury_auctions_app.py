import streamlit as st
import pandas as pd
import requests
from datetime import date, timedelta
import re
from pathlib import Path

st.set_page_config(page_title="Treasury Auction Dashboard", layout="wide")
API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
FIELDS = ["cusip","auction_date","security_type","security_term","inflation_index_security","floating_rate","offering_amt","primary_dealer_accepted","direct_bidder_accepted","indirect_bidder_accepted","comp_accepted","high_yield","high_discnt_rate","bid_to_cover_ratio"]
AMOUNTS = ["offering_amt","primary_dealer_accepted","direct_bidder_accepted","indirect_bidder_accepted"]
NUMERIC = AMOUNTS + ["comp_accepted","high_yield","high_discnt_rate","bid_to_cover_ratio"]

REGULAR_BILL_WEEKS = {4, 6, 8, 13, 17, 26, 52}

def bill_category(term):
    """Classify standard bill tenors separately from nonstandard short-dated bills."""
    label = str(term or "").strip()
    match = re.match(r"^(\d+)\s*[- ]\s*(Week|Day)s?\b", label, re.IGNORECASE)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if (unit == "week" and amount in REGULAR_BILL_WEEKS) or (unit == "day" and amount in {7 * w for w in REGULAR_BILL_WEEKS}):
            return "Regular Treasury Bills"
    return "Cash Management / Other Short-Dated Bills"

def canonical_security(row):
    """Normalize Treasury reopenings to their benchmark tenors."""
    term = str(row["security_term"] or "").strip()
    kind = str(row["security_type"] or "").strip()
    if kind == "Bill":
        return f"{term} Bill".strip()

    match = re.match(r"^(\d+)-Year", term, re.IGNORECASE)
    if not match:
        return f"{term} {kind}".strip()
    years = int(match.group(1))

    if kind == "Note" and years == 9:
        return "10-Year Note"
    if kind == "Bond" and years == 19:
        return "20-Year Bond"
    if kind == "Bond" and years == 29:
        return "30-Year Bond"

    month_match = re.search(r"(\d+)-Month", term, re.IGNORECASE)
    months = int(month_match.group(1)) if month_match else 0
    if months in (10, 11) and years + 1 in {2, 3, 5, 7, 10, 20, 30}:
        return f"{years + 1}-Year {kind}"
    return f"{years}-Year {kind}" if years in {2, 3, 5, 7, 10, 20, 30} else f"{term} {kind}"

@st.cache_data(ttl=900)
def load(days, schema_version=6):
    params = {"fields": ",".join(FIELDS), "filter": f"auction_date:gte:{(date.today()-timedelta(days=days)).isoformat()}", "sort":"-auction_date","page[size]":1000}
    rows = []
    with requests.Session() as session:
        for page in range(1,30):
            params["page[number]"] = page
            response = session.get(API,params=params,timeout=40)
            if not response.ok:
                raise RuntimeError(f"Treasury API HTTP {response.status_code}: {response.text[:700]}")
            payload = response.json()
            batch = payload.get("data",[])
            rows.extend(batch)
            if not batch or len(batch)<1000:
                break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["auction_date"] = pd.to_datetime(df["auction_date"],errors="coerce")
    for col in NUMERIC:
        df[col] = pd.to_numeric(df[col],errors="coerce")
    df = df[df["security_type"].isin(["Bill","Note","Bond"]) & (df["auction_date"].dt.date<=date.today())].copy()
    for flag in ["inflation_index_security","floating_rate"]:
        df = df[~df[flag].fillna("").astype(str).str.lower().isin(["yes","true","1"])]
    df = df[df["bid_to_cover_ratio"].notna()].copy()
    df["Security"] = df.apply(canonical_security, axis=1)
    df["Bill category"] = df["security_term"].apply(bill_category).where(df["security_type"].eq("Bill"), "")
    for label,field in [("Dealer %","primary_dealer_accepted"),("Direct %","direct_bidder_accepted"),("Indirect %","indirect_bidder_accepted")]:
        df[label] = df[field]/df["comp_accepted"].where(df["comp_accepted"]>0)*100
    df["High rate %"] = df["high_discnt_rate"].where(df["security_type"].eq("Bill"),df["high_yield"])
    df["Rate basis"] = df["security_type"].map({"Bill":"Discount rate","Note":"Yield","Bond":"Yield"})
    return df.sort_values("auction_date",ascending=False).drop_duplicates(["cusip","auction_date"])

@st.cache_data(ttl=900)
def load_tails():
    """Monthly nominal auction tails from the Auction Tails master worksheet."""
    path = Path(__file__).with_name("auction_tails.csv")
    tails = pd.read_csv(path, dtype={"auction_month": str, "Security": str})
    tails["tail_bps"] = pd.to_numeric(tails["tail_bps"], errors="coerce")
    return tails.dropna(subset=["tail_bps"]).drop_duplicates(["auction_month", "Security"], keep="last")

def add_tails(frame):
    """Match monthly source data to the official auction date and benchmark tenor."""
    result = frame.copy()
    result["auction_month"] = result["auction_date"].dt.strftime("%Y-%m")
    result = result.merge(load_tails(), how="left", on=["auction_month", "Security"], validate="many_to_one")
    result.loc[result["security_type"].eq("Bill"), "tail_bps"] = float("nan")
    return result

def table(df):
    names = {"auction_date":"Auction date","offering_amt":"Offering (USD bln)","primary_dealer_accepted":"Dealer accepted (USD bln)","direct_bidder_accepted":"Direct accepted (USD bln)","indirect_bidder_accepted":"Indirect accepted (USD bln)","High rate %":"High yield / discount rate (%)","bid_to_cover_ratio":"Bid-to-cover"}
    columns = ["auction_date","Security","offering_amt","primary_dealer_accepted","Dealer %","direct_bidder_accepted","Direct %","indirect_bidder_accepted","Indirect %","High rate %","bid_to_cover_ratio","Rate basis"]
    out = df[columns].rename(columns=names).copy()
    out["Auction date"] = out["Auction date"].dt.strftime("%d/%m/%Y")
    for col in [names[x] for x in AMOUNTS]:
        out[col] /= 1e9
    return out

st.title("US Treasury Auction Dashboard")
st.caption("Official US Treasury FiscalData | Bills, Notes and Bonds | Cached for 15 minutes")
with st.sidebar:
    days = st.selectbox("Recent-auction history", [90,180,365,730,1825,3650], index=4, format_func=lambda n: f"{n} days" if n < 365 else f"{n // 365} years")
    benchmark_days = st.selectbox("Benchmark history (years)", [2,3,5,10], index=2)
    if st.button("Refresh data"):
        load.clear()
        st.rerun()
try:
    data = load(max(days, benchmark_days * 365))
except Exception as exc:
    st.error(f"Treasury data could not be loaded: {exc}")
    st.stop()
if data.empty:
    st.warning("No completed auctions returned.")
    st.stop()
with st.sidebar:
    kinds = st.multiselect("Security types",["Bill","Note","Bond"],default=["Bill","Note","Bond"])
    st.caption("Choose maturities separately inside the Bills and Notes & Bonds tabs.")
    securities = []
    dates = st.date_input("Auction date range", (max(data.auction_date.min().date(), date.today()-timedelta(days=days)), date.today()), min_value=data.auction_date.min().date(), max_value=date.today(), key="full_auction_date_range_v2")
filtered = data[data.security_type.isin(kinds) & (data.auction_date.dt.date >= date.today()-timedelta(days=days))].copy()
if securities:
    filtered = filtered[filtered.Security.isin(securities)]
if len(dates)==2:
    filtered = filtered[filtered.auction_date.dt.date.between(*dates)]


bills_tab, notes_bonds_tab, averages = st.tabs(["Bill auctions", "Notes & bonds", "Auction averages"])

METRICS = {
    "Bid-to-cover": "bid_to_cover_ratio",
    "Indirect %": "Indirect %",
    "Direct %": "Direct %",
    "Dealer %": "Dealer %",
    "High yield / discount rate (%)": "High rate %",
    "Offering (USD bln)": "offering_amt",
    "Tail / stop-through (bps)": "tail_bps",
}
SCALE = {"offering_amt": 1e9}

def show_auction_table(frame, label, filename):
    st.subheader(label)
    st.caption(f"{len(frame):,} auctions shown. Bidder percentages use competitive accepted. Bills show the high discount rate; notes and bonds show high yield.")
    output = table(frame)
    st.dataframe(output, hide_index=True, width="stretch", height=480)
    st.download_button("Download auction history CSV", output.to_csv(index=False).encode("utf-8-sig"), filename, "text/csv")

def show_history_analysis(frame, widget_key):
    st.subheader("Historical demand trends")
    if frame.empty:
        st.info("No auctions available for this maturity and date range.")
        return
    available_metrics = [m for m in METRICS if METRICS[m] != "tail_bps" or frame["tail_bps"].notna().any()]
    metric = st.selectbox("Chart metric", available_metrics, key=f"{widget_key}_metric")
    history_count = st.selectbox("Chart history", [12, 24, 52, 104], index=1, format_func=lambda n: f"Last {n} auctions", key=f"{widget_key}_history")
    moving_count = st.selectbox("Moving average", [3, 6, 10, 12], index=1, format_func=lambda n: f"{n} auctions", key=f"{widget_key}_moving")
    field = METRICS[metric]
    scale = SCALE.get(field, 1)
    # Use all loaded auctions for the selected benchmark, not only the sidebar date range.
    history = frame.sort_values("auction_date").tail(history_count).copy()
    history["Auction"] = history["auction_date"].dt.strftime("%d/%m/%Y")
    history["Latest observations"] = history[field] / scale
    history[f"{moving_count}-auction moving average"] = history["Latest observations"].rolling(moving_count, min_periods=moving_count).mean()
    chart = history.set_index("auction_date")[["Latest observations", f"{moving_count}-auction moving average"]]
    st.line_chart(chart, x_label="Auction date", y_label=metric, width="stretch")
    st.caption("The moving average includes the current auction at each point. Chart observations are ordered by auction date.")
    valid = frame.sort_values("auction_date", ascending=False)[field].dropna()
    if len(valid) < 2:
        st.info("At least two valid auction observations are needed for percentile rankings.")
        return
    latest_value = valid.iloc[0] / scale
    previous = valid.iloc[1:] / scale
    percentile = 100 * (previous.lt(latest_value).sum() + 0.5 * previous.eq(latest_value).sum()) / len(previous)
    a, b, d = st.columns(3)
    a.metric(f"Latest available {metric}", f"{latest_value:,.2f}")
    b.metric("Historical percentile", f"{percentile:.0f}th")
    d.metric("Previous observations", f"{len(previous):,}")
    st.caption("Percentile compares the latest available observation for this metric with earlier observations for the benchmark; 100th means a higher numerical reading. For tails, LOWER is stronger. A higher yield or dealer take-down does not necessarily mean stronger demand. Ties receive half credit.")

with bills_tab:
    st.subheader("Treasury bills")
    bill_categories = st.multiselect(
        "Bill category",
        ["Regular Treasury Bills", "Cash Management / Other Short-Dated Bills"],
        default=["Regular Treasury Bills"], key="bill_category_v2"
    )
    st.caption("Nonstandard maturities are classified by their stated term; this does not confirm that each is officially a Cash Management Bill.")
    bill_pool = data[(data["security_type"] == "Bill") & data["Bill category"].isin(bill_categories)].copy()
    bill_options = sorted(bill_pool["Security"].dropna().unique().tolist(),
                          key=lambda s: (0 if "Week" in s else 1, int(re.search(r"[0-9]+", s).group()) if re.search(r"[0-9]+", s) else 999))
    selected_bill = st.selectbox("Select bill maturity", bill_options, index=0 if bill_options else None,
                                 placeholder="Choose a bill maturity", key="bill_maturity_single_v1")
    if selected_bill:
        chosen_bill = bill_pool[bill_pool["Security"] == selected_bill]
        visible_bill = chosen_bill[chosen_bill.index.isin(filtered.index)]
        show_history_analysis(chosen_bill, "bill")
        show_auction_table(visible_bill, f"{selected_bill} auction history", "treasury_bill_auctions.csv")
    else:
        st.info("Select a bill category with available auctions.")

with notes_bonds_tab:
    st.subheader("Treasury notes & bonds")
    nb_pool = add_tails(data[data["security_type"].isin(["Note", "Bond"]) & data["security_type"].isin(kinds)].copy())
    nb_options = sorted(nb_pool["Security"].dropna().unique().tolist(),
                        key=lambda s: (int(re.search(r"[0-9]+", s).group()) if re.search(r"[0-9]+", s) else 999, s))
    selected_note_bond = st.selectbox("Select note or bond maturity", nb_options,
                                     index=0 if nb_options else None, placeholder="Choose a maturity",
                                     key="note_bond_maturity_single_v1")
    if selected_note_bond:
        chosen_nb = nb_pool[nb_pool["Security"] == selected_note_bond]
        visible_nb = chosen_nb[chosen_nb.index.isin(filtered.index)]
        st.caption("Auction tails are sourced from the uploaded Auction Tails master worksheet (2024–2026), matched by auction month and benchmark maturity. Positive = tail; negative = stop-through; zero = on the screws. Missing values remain blank.")
        show_history_analysis(chosen_nb, "note_bond")
        st.subheader("Tail / stop-through analysis")
        tail_history = chosen_nb.dropna(subset=["tail_bps"]).sort_values("auction_date", ascending=False).copy()
        if tail_history.empty:
            st.info("No recorded tails for this maturity.")
        else:
            tail_window = st.selectbox("Tail average lookback", [3, 6, 10, 12], index=1, format_func=lambda n: f"{n} auctions", key="tail_avg_window_v1")
            current = tail_history.iloc[0]
            preceding = tail_history.iloc[1:tail_window + 1]
            x, y, z = st.columns(3)
            x.metric("Latest recorded tail (bps)", f"{current.tail_bps:+.2f}")
            y.metric(f"Prior {tail_window} average (bps)", f"{preceding.tail_bps.mean():+.2f}" if not preceding.empty else "N/A")
            z.metric("Prior observations", str(len(preceding)))
            st.caption(f"Latest recorded tail: {current.auction_date:%d/%m/%Y}. The historical average excludes that auction. A negative tail is a stop-through.")
            st.dataframe(tail_history[["auction_date", "tail_bps"]].rename(columns={"auction_date": "Auction date", "tail_bps": "Tail / stop-through (bps)"}), hide_index=True, width="stretch")
        show_auction_table(visible_nb, f"{selected_note_bond} auction history", "treasury_notes_bonds_auctions.csv")
    else:
        st.info("No note or bond maturities are available.")

with averages:
    average_count = st.selectbox("Average lookback (preceding auctions)", [3, 6, 10, 12], index=1,
                                 format_func=lambda n: f"{n}-auction average")
    st.subheader(f"Latest auction vs preceding {average_count} auctions")
    st.caption("Select one or all maturities. The latest auction is excluded from its historical average. Notes and bond reopenings are grouped by benchmark tenor.")
    avg_choices = []
    if "Bill" in kinds:
        avg_choices.extend(bill_pool["Security"].dropna().unique().tolist())
    if any(k in kinds for k in ["Note", "Bond"]):
        avg_choices.extend(nb_pool["Security"].dropna().unique().tolist())
    avg_choices = sorted(set(avg_choices))
    default_avg = [x for x in [selected_bill, selected_note_bond] if x in avg_choices]
    avg_selection = st.multiselect("Maturities to compare", avg_choices, default=default_avg, key="avg_maturities_v1")
    metrics = {"Dealer %":"Dealer %","Direct %":"Direct %","Indirect %":"Indirect %","High rate %":"High rate %","Bid-to-cover":"bid_to_cover_ratio","Offering (USD bln)":"offering_amt","Dealer accepted (USD bln)":"primary_dealer_accepted","Direct accepted (USD bln)":"direct_bidder_accepted","Indirect accepted (USD bln)":"indirect_bidder_accepted"}
    records = []
    avg_data = add_tails(data)
    for (kind, term), group in avg_data.groupby(["security_type", "Security"]):
        if term not in avg_selection or kind not in kinds:
            continue
        group = group.sort_values("auction_date", ascending=False)
        latest = group.iloc[0]
        prior = group.iloc[1:average_count + 1]
        if prior.empty:
            continue
        row = {"Security": term, "Latest date": latest.auction_date.strftime("%d/%m/%Y"),
               "Prior auctions": len(prior), "Rate basis": latest["Rate basis"]}
        for label, field in metrics.items():
            scale = 1e9 if "USD bln" in label else 1
            mean = prior[field].mean()
            row["Avg " + label] = mean / scale
            row["Latest " + label] = latest[field] / scale
            row["Difference " + label] = (latest[field] - mean) / scale
        if kind in ("Note", "Bond"):
            recorded = group.dropna(subset=["tail_bps"])
            if not recorded.empty:
                latest_tail = recorded.iloc[0]
                previous_tails = recorded.iloc[1:average_count + 1]["tail_bps"]
                row["Latest tail (bps)"] = latest_tail["tail_bps"]
                row["Tail date"] = latest_tail.auction_date.strftime("%d/%m/%Y")
                row["Prior tail observations"] = len(previous_tails)
                row["Avg tail (bps)"] = previous_tails.mean() if not previous_tails.empty else float("nan")
                row["Difference tail (bps)"] = latest_tail["tail_bps"] - previous_tails.mean() if not previous_tails.empty else float("nan")
        records.append(row)
    if not records:
        st.info("Choose a maturity with at least one preceding auction.")
    else:
        avg = pd.DataFrame(records).sort_values("Security")
        selected_metrics = st.multiselect("Metrics", list(metrics),
                                          default=["Dealer %", "Direct %", "Indirect %", "High rate %", "Bid-to-cover"],
                                          key="avg_metric_selection_v2")
        cols = ["Security", "Latest date", "Prior auctions", "Rate basis"]
        for label in selected_metrics:
            cols += ["Avg " + label, "Latest " + label, "Difference " + label]
        if "Latest tail (bps)" in avg.columns:
            cols += ["Tail date", "Prior tail observations", "Avg tail (bps)", "Latest tail (bps)", "Difference tail (bps)"]
        st.dataframe(avg[cols], hide_index=True, width="stretch", height=620)
        st.download_button(f"Download {average_count}-auction averages CSV",
                           avg.to_csv(index=False).encode("utf-8-sig"),
                           f"treasury_{average_count}_auction_averages.csv", "text/csv")
        st.caption(f"Differences are latest minus the preceding-{average_count} average; bidder share differences are percentage points. Prior auctions shows the actual available sample.")
