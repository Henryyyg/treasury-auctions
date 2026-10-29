import streamlit as st
import pandas as pd
import requests
from datetime import date, timedelta
import re
import base64
import io
from pathlib import Path

st.set_page_config(page_title="Treasury Auction Dashboard", layout="wide")
API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
FIELDS = ["cusip","auction_date","security_type","security_term","inflation_index_security","floating_rate","offering_amt","primary_dealer_accepted","direct_bidder_accepted","indirect_bidder_accepted","comp_accepted","high_yield","high_discnt_rate","high_discnt_margin","bid_to_cover_ratio"]
AMOUNTS = ["offering_amt","primary_dealer_accepted","direct_bidder_accepted","indirect_bidder_accepted"]
NUMERIC = AMOUNTS + ["comp_accepted","high_yield","high_discnt_rate","high_discnt_margin","bid_to_cover_ratio"]

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
    if kind == "FRN":
        return "2-Year FRN"
    if kind == "TIPS":
        match = re.match(r"^(\d+)-Year", term, re.IGNORECASE)
        if not match:
            return f"{term} TIPS"
        years = int(match.group(1))
        month_match = re.search(r"(\d+)-Month", term, re.IGNORECASE)
        months = int(month_match.group(1)) if month_match else 0
        if years in (5, 10, 30) and months == 0:
            benchmark = years
        elif years + 1 in (5, 10, 30) and months > 0:
            benchmark = years + 1
        else:
            benchmark = min((5, 10, 30), key=lambda x: abs(x - (years + months / 12)))
        return f"{benchmark}-Year TIPS"

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
def load(days, schema_version=7):
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
    df = df[df["security_type"].isin(["Bill","Note","Bond","TIPS","FRN"]) & (df["auction_date"].dt.date<=date.today())].copy()
    tips = df["inflation_index_security"].fillna("").astype(str).str.lower().isin(["yes","true","1"]) | df["security_type"].eq("TIPS")
    frns = df["floating_rate"].fillna("").astype(str).str.lower().isin(["yes","true","1"]) | df["security_type"].eq("FRN")
    df["Auction class"] = "Nominal"
    df.loc[tips, "Auction class"] = "TIPS"
    df.loc[frns & ~tips, "Auction class"] = "FRN"
    df.loc[tips, "security_type"] = "TIPS"
    df.loc[frns & ~tips, "security_type"] = "FRN"
    df = df[df["bid_to_cover_ratio"].notna()].copy()
    df["Security"] = df.apply(canonical_security, axis=1)
    df["Bill category"] = df["security_term"].apply(bill_category).where(df["security_type"].eq("Bill"), "")
    for label,field in [("Dealer %","primary_dealer_accepted"),("Direct %","direct_bidder_accepted"),("Indirect %","indirect_bidder_accepted")]:
        df[label] = df[field]/df["comp_accepted"].where(df["comp_accepted"]>0)*100
    df["High rate %"] = df["high_yield"]
    df.loc[df["security_type"].eq("Bill"), "High rate %"] = df["high_discnt_rate"]
    df.loc[df["Auction class"].eq("FRN"), "High rate %"] = df["high_discnt_margin"]
    df["Rate basis"] = df["security_type"].map({"Bill":"Discount rate","Note":"Yield","Bond":"Yield","TIPS":"Real yield","FRN":"Discount margin"})
    return df.sort_values("auction_date",ascending=False).drop_duplicates(["cusip","auction_date"])

@st.cache_data(ttl=60)
def load_tails():
    """Read the latest saved tails from GitHub, falling back to bundled CSV."""
    path = Path(__file__).with_name("auction_tails.csv")
    try:
        url = "https://raw.githubusercontent.com/Henryyyg/treasury-auctions/main/auction_tails.csv"
        response = requests.get(url, params={"refresh": int(pd.Timestamp.now().timestamp() // 60)}, timeout=12)
        response.raise_for_status()
        tails = pd.read_csv(io.StringIO(response.text), dtype={"auction_month": str, "Security": str})
    except (requests.RequestException, ValueError, pd.errors.ParserError):
        tails = pd.read_csv(path, dtype={"auction_month": str, "Security": str})
    tails["tail_bps"] = pd.to_numeric(tails["tail_bps"], errors="coerce")
    return tails.dropna(subset=["tail_bps"]).drop_duplicates(["auction_month", "Security"], keep="last")

def add_tails(frame):
    """Match monthly source data to the official auction date and benchmark tenor."""
    result = frame.copy()
    result["auction_month"] = result["auction_date"].dt.strftime("%Y-%m")
    result = result.merge(load_tails(), how="left", on=["auction_month", "Security"], validate="many_to_one")
    result.loc[result["security_type"].isin(["Bill", "FRN"]), "tail_bps"] = float("nan")
    # TIPS tails transcribed from the TIPS worksheet (2025 onward).
    # Blank and zero spreadsheet placeholders indicate no auction.
    tips_history = {
        ("2025-01", "10-Year TIPS"): 1.1,
        ("2025-02", "30-Year TIPS"): 0.3,
        ("2025-03", "10-Year TIPS"): 0.5,
        ("2025-04", "5-Year TIPS"): 2.2,
        ("2025-06", "5-Year TIPS"): -1.0,
        ("2025-07", "10-Year TIPS"): -0.5,
        ("2025-08", "30-Year TIPS"): -2.3,
        ("2025-09", "10-Year TIPS"): 5.0,
        ("2025-10", "5-Year TIPS"): 1.2,
        ("2025-11", "10-Year TIPS"): 1.4,
        ("2025-12", "5-Year TIPS"): -0.7,
        ("2026-01", "10-Year TIPS"): 2.0,
        ("2026-02", "30-Year TIPS"): -1.7,
        ("2026-03", "10-Year TIPS"): 1.6,
        ("2026-04", "5-Year TIPS"): 0.2,
        ("2026-05", "10-Year TIPS"): 1.9,
        ("2026-06", "5-Year TIPS"): -0.5,
        ("2026-07", "10-Year TIPS"): 2.8,
        ("2026-08", "30-Year TIPS"): -1.8,
    }
    tips_mask = result["Auction class"].eq("TIPS")
    result.loc[tips_mask, "tail_bps"] = [
        saved if pd.notna(saved) else tips_history.get((month, security), float("nan"))
        for month, security, saved in zip(result.loc[tips_mask, "auction_month"], result.loc[tips_mask, "Security"], result.loc[tips_mask, "tail_bps"])
    ]
    return result

def table(df):
    names = {"auction_date":"Auction date","offering_amt":"Offering (USD bln)","primary_dealer_accepted":"Dealer accepted (USD bln)","direct_bidder_accepted":"Direct accepted (USD bln)","indirect_bidder_accepted":"Indirect accepted (USD bln)","High rate %":"High yield / discount rate (%)","bid_to_cover_ratio":"Bid-to-cover"}
    columns = ["auction_date","Security","offering_amt","primary_dealer_accepted","Dealer %","direct_bidder_accepted","Direct %","indirect_bidder_accepted","Indirect %","High rate %","bid_to_cover_ratio","Rate basis"]
    columns += ["tail_bps"] if "tail_bps" in df.columns else []
    out = df[columns].rename(columns={**names, "tail_bps": "Tail / stop-through (bps)"}).copy()
    if "Tail / stop-through (bps)" not in out.columns:
        out["Tail / stop-through (bps)"] = float("nan")
    out["Auction date"] = out["Auction date"].dt.strftime("%d/%m/%Y")
    for col in [names[x] for x in AMOUNTS]:
        out[col] /= 1e9
    return out

st.title("US Treasury Auction Dashboard")
st.caption("Official US Treasury FiscalData | Bills, Notes, Bonds, TIPS and FRNs | Cached for 15 minutes")
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
st.caption(f"Latest completed auction returned by Treasury: {data.auction_date.max():%d/%m/%Y}. If newer results are missing, try Refresh data; Treasury may not have published them to FiscalData yet.")
with st.sidebar:
    kinds = st.multiselect("Security types",["Bill","Note","Bond","TIPS","FRN"],default=["Bill","Note","Bond","TIPS","FRN"])
    st.caption("Choose maturities separately inside the Bills and Notes & Bonds tabs.")
    securities = []
    custom_dates = st.checkbox("Use custom auction date range", value=False, key="custom_auction_dates_v1")
    if custom_dates:
        dates = st.date_input("Auction date range", (max(data.auction_date.min().date(), date.today()-timedelta(days=days)), date.today()), min_value=data.auction_date.min().date(), max_value=date.today(), key="custom_auction_date_range_v3")
filtered = data[data.security_type.isin(kinds) & (data.auction_date.dt.date >= date.today()-timedelta(days=days))].copy()
if securities:
    filtered = filtered[filtered.Security.isin(securities)]
if custom_dates and len(dates)==2:
    filtered = filtered[filtered.auction_date.dt.date.between(*dates)]


bills_tab, notes_bonds_tab, tips_tab, frn_tab, averages, update_tails_tab = st.tabs(["Bill auctions", "Notes & bonds", "TIPS", "FRNs", "Auction averages", "Update auction tails"])

METRICS = {
    "Tail / stop-through (bps)": "tail_bps",
    "Bid-to-cover": "bid_to_cover_ratio",
    "Indirect %": "Indirect %",
    "Direct %": "Direct %",
    "Dealer %": "Dealer %",
    "High yield / discount margin (%)": "High rate %",
    "Offering (USD bln)": "offering_amt",
}
SCALE = {"offering_amt": 1e9}

def show_auction_table(frame, label, filename):
    st.subheader(label)
    st.caption(f"{len(frame):,} auctions shown. Bidder percentages use competitive accepted. Bills show the high discount rate; notes and bonds show high yield.")
    output = table(frame)
    for percentage_column in ["Dealer %", "Direct %", "Indirect %"]:
        output[percentage_column] = output[percentage_column].round(2)
    numeric_cols = ["Auction date", "Security", "Offering (USD bln)", "Dealer accepted (USD bln)", "Direct accepted (USD bln)", "Indirect accepted (USD bln)", "High yield / discount rate (%)", "Tail / stop-through (bps)", "Bid-to-cover"]
    percentage_cols = ["Auction date", "Security", "Tail / stop-through (bps)", "Bid-to-cover", "Dealer %", "Direct %", "Indirect %"]
    st.markdown("**Auction Results**")
    st.dataframe(output[percentage_cols], hide_index=True, width="stretch", height=360,
                 column_config={name: st.column_config.NumberColumn(name, format="%.2f%%") for name in ["Dealer %", "Direct %", "Indirect %"]})
    st.markdown("**Auction amounts**")
    st.dataframe(output[numeric_cols], hide_index=True, width="stretch", height=360)
    st.download_button("Download full auction history CSV", output.to_csv(index=False).encode("utf-8-sig"), filename, "text/csv")

def show_history_analysis(frame, widget_key):
    st.subheader("Historical demand trends")
    if frame.empty:
        st.info("No auctions available for this maturity and date range.")
        return
    has_tail_data = "tail_bps" in frame.columns and frame["tail_bps"].notna().any()
    available_metrics = [m for m in METRICS if METRICS[m] != "tail_bps" or has_tail_data]
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
        # A merge in add_tails resets row indices; filter by auction identity, never DataFrame index.
        visible_nb = chosen_nb.merge(filtered[["cusip", "auction_date"]].drop_duplicates(), on=["cusip", "auction_date"], how="inner")
        show_history_analysis(chosen_nb, "note_bond")
        show_auction_table(visible_nb, f"{selected_note_bond} auction history", "treasury_notes_bonds_auctions.csv")
    else:
        st.info("No note or bond maturities are available.")

with tips_tab:
    st.subheader("Treasury Inflation-Protected Securities (TIPS)")
    tips_pool = add_tails(data[data["Auction class"].eq("TIPS") & data["security_type"].isin(kinds)].copy())
    tips_options = sorted(tips_pool["Security"].dropna().unique().tolist())
    selected_tips = st.selectbox("TIPS benchmark", tips_options, index=0 if tips_options else None, key="tips_tenor")
    if selected_tips:
        chosen = tips_pool[tips_pool["Security"].eq(selected_tips)]
        visible = chosen.merge(filtered[["cusip", "auction_date"]].drop_duplicates(), on=["cusip", "auction_date"], how="inner")
        st.caption("Reopenings grouped by original 5-, 10- or 30-year TIPS benchmark; high yield is real yield.")
        show_history_analysis(chosen, "tips")
        show_auction_table(visible, f"{selected_tips} auction history", "treasury_tips.csv")
    else:
        st.info("No TIPS auctions available.")

with frn_tab:
    st.subheader("Floating Rate Notes (FRNs)")
    frn_pool = data[data["Auction class"].eq("FRN") & data["security_type"].isin(kinds)].copy()
    frn_options = sorted(frn_pool["Security"].dropna().unique().tolist())
    selected_frn = st.selectbox("FRN maturity", frn_options, index=0 if frn_options else None, key="frn_tenor")
    if selected_frn:
        chosen = frn_pool[frn_pool["Security"].eq(selected_frn)]
        visible = chosen.merge(filtered[["cusip", "auction_date"]].drop_duplicates(), on=["cusip", "auction_date"], how="inner")
        st.caption("FRN high discount margin replaces high yield. Reopenings grouped under 2-Year FRN.")
        show_history_analysis(chosen, "frn")
        show_auction_table(visible, f"{selected_frn} auction history", "treasury_frn.csv")
    else:
        st.info("No FRN auctions available.")

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
    if "FRN" in kinds:
        avg_choices.extend(frn_pool["Security"].dropna().unique().tolist())
    avg_choices = sorted(set(avg_choices))
    default_avg = [x for x in [selected_bill, selected_note_bond, selected_tips, selected_frn] if x in avg_choices]
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
        selected_metrics = st.multiselect("Metrics", ["Tail / stop-through (bps)"] + list(metrics),
                                          default=["Dealer %", "Direct %", "Indirect %", "High rate %", "Bid-to-cover"],
                                          key="avg_metric_selection_v2")
        cols = ["Security", "Latest date", "Prior auctions", "Rate basis"]
        for label in selected_metrics:
            cols += ["Avg " + label, "Latest " + label, "Difference " + label]
        if "Tail / stop-through (bps)" in selected_metrics and "Latest tail (bps)" in avg.columns:
            cols += ["Tail date", "Prior tail observations", "Avg tail (bps)", "Latest tail (bps)", "Difference tail (bps)"]
        st.dataframe(avg[cols], hide_index=True, width="stretch", height=620)
        st.download_button(f"Download {average_count}-auction averages CSV",
                           avg.to_csv(index=False).encode("utf-8-sig"),
                           f"treasury_{average_count}_auction_averages.csv", "text/csv")
        st.caption(f"Differences are latest minus the preceding-{average_count} average; bidder share differences are percentage points. Prior auctions shows the actual available sample.")


with update_tails_tab:
    st.subheader("Enter or correct an auction tail")
    st.caption("Nominal and TIPS tails are saved to auction_tails.csv in GitHub. Positive = tail, negative = stop-through, zero = on the screws. One entry per maturity per month. Saved TIPS entries override the imported historical figures.")
    tenor_options = ["3-Year Note", "10-Year Note", "30-Year Bond", "20-Year Bond", "2-Year Note", "5-Year Note", "7-Year Note", "5-Year TIPS", "10-Year TIPS", "30-Year TIPS"]
    today = date.today()
    with st.form("tail_entry_form", clear_on_submit=False):
        tenor = st.selectbox("Auction maturity", tenor_options)
        auction_month_date = st.date_input("Auction month (choose any date in the month)", value=today, min_value=date(2024, 1, 1), max_value=today)
        tail_value = st.number_input("Tail / stop-through (bps)", value=0.0, step=0.1, format="%.2f")
        confirm_overwrite = st.checkbox("Allow replacement if this month's figure already exists")
        submitted = st.form_submit_button("Save tail", type="primary")
    month_key = auction_month_date.strftime("%Y-%m")
    existing = load_tails()
    matching = existing[(existing["auction_month"] == month_key) & (existing["Security"] == tenor)]
    if not matching.empty:
        st.info(f"Currently saved for {tenor}, {month_key}: {matching.iloc[0]['tail_bps']:+.2f}bps.")
    elif tenor.endswith("TIPS"):
        st.caption("Historical TIPS figures from the Excel import are shown until you save an updated value for the same month and maturity.")
    if submitted:
        if not matching.empty and not confirm_overwrite:
            st.warning("An entry already exists. Tick the replacement checkbox to update it.")
        else:
            token = st.secrets.get("GITHUB_TOKEN", "")
            if not token:
                st.error("Saving is not configured yet. Add GITHUB_TOKEN to Streamlit app Secrets (Settings → Secrets).")
            else:
                repo_path = "Henryyyg/treasury-auctions"
                api_url = f"https://api.github.com/repos/{repo_path}/contents/auction_tails.csv"
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                           "X-GitHub-Api-Version": "2022-11-28"}
                try:
                    with requests.Session() as session:
                        response = session.get(api_url, headers=headers, timeout=20)
                        response.raise_for_status()
                        github_file = response.json()
                        raw_csv = base64.b64decode(github_file["content"]).decode("utf-8-sig")
                        live = pd.read_csv(io.StringIO(raw_csv), dtype={"auction_month": str, "Security": str})
                        already = (live["auction_month"].eq(month_key) & live["Security"].eq(tenor))
                        if already.any() and not confirm_overwrite:
                            st.warning("The entry already exists in GitHub. Tick the replacement checkbox to update it.")
                        else:
                            live = live.loc[~already, ["auction_month", "Security", "tail_bps"]].copy()
                            new_row = pd.DataFrame([{"auction_month": month_key, "Security": tenor, "tail_bps": tail_value}])
                            live = pd.concat([live, new_row], ignore_index=True).sort_values(["auction_month", "Security"])
                            csv_text = live.to_csv(index=False, lineterminator="\n")
                            payload = {"message": f"Update {tenor} tail for {month_key}",
                                       "content": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
                                       "sha": github_file["sha"], "branch": "main"}
                            saved = session.put(api_url, headers=headers, json=payload, timeout=25)
                            saved.raise_for_status()
                            load_tails.clear()
                            st.success(f"Saved {tenor}, {month_key}: {tail_value:+.2f}bps to GitHub. Charts will use the new value after refresh.")
                            st.caption("Streamlit Cloud may briefly redeploy after the GitHub commit.")
                except requests.HTTPError as exc:
                    code = exc.response.status_code if exc.response is not None else "unknown"
                    st.error(f"GitHub could not save the figure (HTTP {code}). Check that the token has Contents: Read and write permission for this repository.")
                except (requests.RequestException, ValueError, KeyError) as exc:
                    st.error(f"Could not save the figure: {type(exc).__name__}. Please retry; no change was confirmed.")
    st.divider()
    st.subheader("Recently saved tails")
    st.caption("Shows the most recent entries saved in GitHub. Updates normally appear within about one minute.")
    recent = load_tails().sort_values(["auction_month", "Security"], ascending=[False, True]).head(21).copy()
    st.dataframe(recent.rename(columns={"auction_month": "Auction month", "Security": "Maturity", "tail_bps": "Tail (bps)"}), hide_index=True, width="stretch")


# Keep the headline generator below every other dashboard section.
st.divider()
with st.expander("Auction headline generator", expanded=False):
    st.caption("Previous completed auction versus the average of the six most recent completed auctions (including the previous auction). Tail observations are maintained separately and may have a different latest date.")
    headline_tenors = ["3-Year Note", "10-Year Note", "30-Year Bond", "20-Year Bond", "2-Year Note", "5-Year Note", "7-Year Note"]
    headline_tenor = st.selectbox("Auction maturity for headline", headline_tenors, key="headline_maturity")
    headline_data = add_tails(data[data["security_type"].isin(["Note", "Bond"])].copy())
    headline_group = headline_data[headline_data["Security"].eq(headline_tenor)].sort_values("auction_date", ascending=False)
    if headline_group.empty:
        st.info("No completed Treasury auctions are available for this maturity.")
    else:
        latest_auction = headline_group.iloc[0]
        prior_six = headline_group.iloc[:6]  # Six most recent completed auctions, including latest
        tails_for_tenor = headline_group.dropna(subset=["tail_bps"]).sort_values("auction_date", ascending=False)
        headline_label = headline_tenor.split("-Year")[0] + "-year"

        def headline_number(value, suffix="", decimals=1):
            return f"{value:.{decimals}f}{suffix}" if pd.notna(value) else "N/A"

        def headline_row(label, latest_value, older, suffix="", decimals=1):
            avg = older.mean() if not older.empty else float("nan")
            return f"- {label}: (prev. {headline_number(latest_value, suffix, decimals)}, six-auction average {headline_number(avg, suffix, decimals)})"

        if tails_for_tenor.empty:
            tail_line = "- Tail: (prev. N/A, six-auction average N/A)"
            latest_tail_date = None
        else:
            latest_tail = tails_for_tenor.iloc[0]
            older_tails = tails_for_tenor.iloc[:6]["tail_bps"]
            tail_line = headline_row("Tail", latest_tail["tail_bps"], older_tails, "bps", 1)
            latest_tail_date = latest_tail["auction_date"]

        headline_lines = [
            f"**{headline_label}**",
            "",
            tail_line,
            headline_row("High Yield", latest_auction["High rate %"], prior_six["High rate %"], "%", 3),
            headline_row("B/C", latest_auction["bid_to_cover_ratio"], prior_six["bid_to_cover_ratio"], "x", 2),
            headline_row("Dealer", latest_auction["Dealer %"], prior_six["Dealer %"], "%", 2),
            headline_row("Direct", latest_auction["Direct %"], prior_six["Direct %"], "%", 2),
            headline_row("Indirect", latest_auction["Indirect %"], prior_six["Indirect %"], "%", 2),
        ]
        headline_text = "\n".join(headline_lines)
        st.markdown(headline_text)
        st.code(headline_text, language=None)
        st.caption(f"Latest completed {headline_tenor} auction: {latest_auction['auction_date']:%d/%m/%Y}. "
                   + (f"Latest recorded tail: {latest_tail_date:%d/%m/%Y}. " if latest_tail_date is not None else "No recorded tail. ")
                   + f"Six-auction averages include the displayed previous observation; {len(prior_six)} completed Treasury auctions and "
                   + f"{len(tails_for_tenor)} recorded tail observations are available.")
        if len(prior_six) < 6 or (not tails_for_tenor.empty and len(tails_for_tenor) < 6):
            st.warning("Fewer than six completed observations are available for at least one metric; averages use the available observations.")
