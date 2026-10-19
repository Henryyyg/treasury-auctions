import streamlit as st
import pandas as pd
import requests
from datetime import date, timedelta
import re

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

bills_tab, notes_bonds_tab, averages = st.tabs(["Bill auctions", "Notes & bonds", "Six-auction averages"])

def show_auction_table(frame, label, filename):
    st.subheader(label)
    st.caption(f"{len(frame):,} auctions shown. Bidder percentages use total competitive accepted. Bills display high discount rate; notes and bonds display high yield.")
    output = table(frame)
    st.dataframe(output, hide_index=True, width="stretch", height=620)
    st.download_button("Download CSV", output.to_csv(index=False).encode("utf-8-sig"), filename, "text/csv")

with bills_tab:
    bill_data = filtered[filtered["security_type"].eq("Bill")]
    bill_categories = st.multiselect("Bill category", ["Regular Treasury Bills", "Cash Management / Other Short-Dated Bills"], default=["Regular Treasury Bills"], key="bill_category_v1")
    bill_data = bill_data[bill_data["Bill category"].isin(bill_categories)]
    st.caption("Other short-dated bills are classified by their stated term; this does not confirm that every such auction is a Cash Management Bill.")
    bill_terms = sorted(bill_data["Security"].dropna().unique().tolist())
    selected_bills = st.multiselect("Filter bill maturities", bill_terms, default=bill_terms, key="normalized_bill_filter_v2")
    show_auction_table(bill_data[bill_data["Security"].isin(selected_bills)], "Treasury bill auctions", "treasury_bill_auctions.csv")

with notes_bonds_tab:
    note_bond_data = filtered[filtered["security_type"].isin(["Note", "Bond"])]
    note_bond_terms = sorted(note_bond_data["Security"].dropna().unique().tolist())
    selected_note_bonds = st.multiselect("Filter note and bond maturities", note_bond_terms, default=note_bond_terms, key="normalized_note_bond_filter_v2")
    show_auction_table(note_bond_data[note_bond_data["Security"].isin(selected_note_bonds)], "Treasury note and bond auctions", "treasury_notes_bonds_auctions.csv")

with averages:
    st.subheader("Latest auction vs preceding six auctions")
    st.caption("Benchmarks use the preceding six auctions within each normalized security. Bills respect the selected bill category; note/bond reopenings are grouped by benchmark tenor. Benchmark history is independent of recent-auction date filters.")
    metrics = {"Dealer %":"Dealer %","Direct %":"Direct %","Indirect %":"Indirect %","High rate %":"High rate %","Bid-to-cover":"bid_to_cover_ratio","Offering (USD bln)":"offering_amt","Dealer accepted (USD bln)":"primary_dealer_accepted","Direct accepted (USD bln)":"direct_bidder_accepted","Indirect accepted (USD bln)":"indirect_bidder_accepted"}
    records=[]
    for (kind,term),group in data.groupby(["security_type","Security"]):
        group=group.sort_values("auction_date",ascending=False)
        latest=group.iloc[0]
        if kind not in kinds or (kind == "Bill" and (latest.Security not in selected_bills or latest["Bill category"] not in bill_categories)) or (kind in ["Note", "Bond"] and latest.Security not in selected_note_bonds):
            continue
        prior=group.iloc[1:7]
        if prior.empty:
            continue
        row={"Security":latest.Security,"Latest date":latest.auction_date.strftime("%d/%m/%Y"),"Prior auctions":len(prior),"Rate basis":latest["Rate basis"]}
        for label,field in metrics.items():
            scale=1e9 if "USD bln" in label else 1
            row["Avg "+label]=prior[field].mean()/scale
            row["Latest "+label]=latest[field]/scale
            row["Difference "+label]=(latest[field]-prior[field].mean())/scale
        records.append(row)
    if not records:
        st.info("No benchmark observations found.")
    else:
        avg=pd.DataFrame(records).sort_values("Security")
        selected=st.multiselect("Metrics",list(metrics),default=["Dealer %","Direct %","Indirect %","High rate %","Bid-to-cover"])
        cols=["Security","Latest date","Prior auctions","Rate basis"]
        for label in selected:
            cols += ["Avg "+label,"Latest "+label,"Difference "+label]
        st.dataframe(avg[cols],hide_index=True,width="stretch",height=620)
        st.download_button("Download six-auction averages CSV",avg.to_csv(index=False).encode("utf-8-sig"),"treasury_six_auction_averages.csv","text/csv")
        st.caption("Differences are latest minus the preceding-six average; bidder share differences are percentage points. A smaller available sample is indicated by Prior auctions.")
