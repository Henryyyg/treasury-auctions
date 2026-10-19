import streamlit as st
import pandas as pd
import requests
from datetime import date, timedelta

st.set_page_config(page_title="Treasury Auction Dashboard", layout="wide")
API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
FIELDS = ["cusip","auction_date","security_type","security_term","inflation_index_security","floating_rate","offering_amt","primary_dealer_accepted","direct_bidder_accepted","indirect_bidder_accepted","comp_accepted","high_yield","high_discnt_rate","bid_to_cover_ratio"]
AMOUNTS = ["offering_amt","primary_dealer_accepted","direct_bidder_accepted","indirect_bidder_accepted"]
NUMERIC = AMOUNTS + ["comp_accepted","high_yield","high_discnt_rate","bid_to_cover_ratio"]

@st.cache_data(ttl=900)
def load(days):
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
    df["Security"] = df["security_term"].fillna("")+" "+df["security_type"]
    df["Security"] = df["Security"].str.strip()
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
    days = st.selectbox("Load history (days)",[90,180,365,730],index=2)
    if st.button("Refresh data"):
        load.clear()
        st.rerun()
try:
    data = load(days)
except Exception as exc:
    st.error(f"Treasury data could not be loaded: {exc}")
    st.stop()
if data.empty:
    st.warning("No completed auctions returned.")
    st.stop()
with st.sidebar:
    kinds = st.multiselect("Security types",["Bill","Note","Bond"],default=["Bill","Note","Bond"])
    securities = st.multiselect("Security maturities (optional)",sorted(data[data.security_type.isin(kinds)]["Security"].unique()))
    dates = st.date_input("Auction date range",(max(data.auction_date.min().date(),date.today()-timedelta(days=90)),date.today()),min_value=data.auction_date.min().date(),max_value=date.today())
filtered = data[data.security_type.isin(kinds)].copy()
if securities:
    filtered = filtered[filtered.Security.isin(securities)]
if len(dates)==2:
    filtered = filtered[filtered.auction_date.dt.date.between(*dates)]

bills_tab, notes_bonds_tab, averages = st.tabs(["Bill auctions", "Notes & bonds", "Six-auction averages"])

def show_auction_table(frame, label, filename):
    st.subheader(label)
    st.caption("Bidder percentages use total competitive accepted. Bills display the high discount rate; notes and bonds display the high yield.")
    output = table(frame)
    st.dataframe(output, hide_index=True, use_container_width=True, height=620)
    st.download_button("Download CSV", output.to_csv(index=False).encode("utf-8-sig"), filename, "text/csv")

with bills_tab:
    show_auction_table(filtered[filtered["security_type"].eq("Bill")], "Treasury bill auctions", "treasury_bill_auctions.csv")

with notes_bonds_tab:
    show_auction_table(filtered[filtered["security_type"].isin(["Note", "Bond"])], "Treasury note and bond auctions", "treasury_notes_bonds_auctions.csv")

with averages:
    st.subheader("Latest auction vs preceding six auctions")
    st.caption("Benchmarks use the preceding six auctions of the same security term, including reopenings. The date-range filter does not restrict the benchmark history.")
    metrics = {"Dealer %":"Dealer %","Direct %":"Direct %","Indirect %":"Indirect %","High rate %":"High rate %","Bid-to-cover":"bid_to_cover_ratio","Offering (USD bln)":"offering_amt","Dealer accepted (USD bln)":"primary_dealer_accepted","Direct accepted (USD bln)":"direct_bidder_accepted","Indirect accepted (USD bln)":"indirect_bidder_accepted"}
    records=[]
    for (kind,term),group in data.groupby(["security_type","security_term"]):
        group=group.sort_values("auction_date",ascending=False)
        latest=group.iloc[0]
        if kind not in kinds or (securities and latest.Security not in securities):
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
        st.dataframe(avg[cols],hide_index=True,use_container_width=True,height=620)
        st.download_button("Download six-auction averages CSV",avg.to_csv(index=False).encode("utf-8-sig"),"treasury_six_auction_averages.csv","text/csv")
        st.caption("Differences are latest minus the preceding-six average; bidder share differences are percentage points. A smaller available sample is indicated by Prior auctions.")
