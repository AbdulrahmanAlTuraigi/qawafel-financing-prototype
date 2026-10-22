"""Turns the SME's raw uploads into the numbers a bank needs.

Implements the metric definitions from the technical spec (section 2.1.1):
clean -> classify bank credits -> match payments to invoices -> metrics -> flags.
Every step records what it changed, so the dashboard can show its work.
"""

import json
import re
from dataclasses import dataclass, field

import pandas as pd

VAT_RATE = 0.15

# Thresholds. ASSUMPTION: starting values, to be tuned with the bank partner.
GAP_12M_RED = 0.10
GAP_ROLLING_AMBER = 0.20
LARGEST_CUSTOMER_AMBER = 0.30
OVERDUE_DAYS = 60
OVERDUE_CUSTOMER_RED = 100_000
DSCR_AMBER = 1.25
DSCR_RED = 1.00
PROFIT_RATE = 0.10          # ASSUMPTION: annual profit rate for the new financing

CATEGORY_RULES = [          # first match wins; checked on the upper-cased description
    ("FINANCE DISBURSEMENT", "loan"),
    ("OWN ACC", "own_transfer"),
    ("(OWNER)", "owner_injection"),
    ("PERSONAL ACC", "owner_injection"),
    ("REFUND", "refund"),
    ("CASH DEPOSIT", "customer_payment"),
    ("SADAD", "customer_payment"),
    ("INCOMING TRF", "customer_payment"),
    ("FINANCE INSTALLMENT", "loan_repayment"),
    ("SUPPLIER", "supplier"),
    ("PAYROLL", "payroll"),
    ("GOSI", "payroll"),
    ("ELECTRICITY", "utilities"),
    ("RENT", "rent"),
    ("ZATCA", "tax"),
]
OPERATING_OUT = {"supplier", "payroll", "utilities", "rent", "tax"}


@dataclass
class Results:
    company: dict
    invoices: pd.DataFrame
    bank: pd.DataFrame
    monthly: pd.DataFrame
    rolling: pd.DataFrame
    customers: pd.DataFrame
    aging: pd.DataFrame
    metrics: dict
    flags: list
    data_quality: list = field(default_factory=list)


def _norm_name(name):
    if not isinstance(name, str):
        return ""
    s = name.lower()
    s = re.sub(r"\b(co|est|trading|trd|company|ltd|mkt|market)\b\.?", "", s)
    return re.sub(r"[^a-z0-9؀-ۿ]", "", s)


def _parse_amount(v):
    if isinstance(v, str):
        return float(re.sub(r"[^0-9.\-]", "", v))
    return float(v)


# ---------------------------------------------------------------- invoices

def clean_invoices(raw):
    df = raw.copy()
    df["source_row"] = df.index + 2            # Excel row number (row 1 is the header)
    log = []

    # A2: amounts typed as text, e.g. "SAR 12,500.00"
    text_mask = df["Net Amount (SAR)"].apply(lambda v: isinstance(v, str))
    df["net"] = df["Net Amount (SAR)"].apply(_parse_amount)
    if text_mask.any():
        log.append(("warn", "A2", f"{text_mask.sum()} amounts were typed as text and converted",
                    df.loc[text_mask, "source_row"].tolist()))

    df["vat"] = df["VAT (SAR)"].astype(float)
    df["gross"] = (df["net"] + df["vat"]).round(2)
    df["issue_date"] = pd.to_datetime(df["Invoice Date"])
    df["file_paid_date"] = pd.to_datetime(df["Paid Date"])
    df["due_date"] = pd.to_datetime(df["Due Date"])
    df["is_credit_note"] = df["Type"].str.lower().str.contains("credit")

    # C1: VAT should be ~15% of net
    bad_vat = (df["vat"] - df["net"] * VAT_RATE).abs() > 1
    if bad_vat.any():
        log.append(("warn", "C1", f"{bad_vat.sum()} rows where VAT is not 15% of net", df.loc[bad_vat, "source_row"].tolist()))

    # C1: duplicates (same invoice number twice). Keep the first, exclude the rest.
    df["is_duplicate"] = df.duplicated(subset=["Invoice No"], keep="first")
    if df["is_duplicate"].any():
        log.append(("warn", "C1", f"{df['is_duplicate'].sum()} duplicate invoices excluded from all totals",
                    df.loc[df["is_duplicate"], "source_row"].tolist()))

    # B1 + C2: customer identity. Group by VAT number when present, otherwise by normalised name.
    missing = df["Customer"].isna()
    df["customer_key"] = df["Customer VAT No"].fillna("").astype(str)
    no_vat = df["customer_key"] == ""
    df.loc[no_vat, "customer_key"] = "name:" + df.loc[no_vat, "Customer"].apply(_norm_name)
    def display_name(s):
        latin = s[~s.str.contains(r"[\u0600-\u06ff]")]      # prefer a Latin spelling for display
        pool = latin if len(latin) else s
        return max(pool.unique(), key=len)                      # the fullest spelling, e.g. not "AlNoor Hyper Mkt"
    names = df.dropna(subset=["Customer"]).groupby("customer_key")["Customer"].agg(display_name)
    df["customer"] = df["customer_key"].map(names)
    recovered = missing & df["customer"].notna()
    if recovered.any():
        log.append(("warn", "B1", f"{recovered.sum()} rows had no customer name; recovered from the VAT number",
                    df.loc[recovered, "source_row"].tolist()))
    unknown = df["customer"].isna()
    df.loc[unknown, "customer"] = "Unknown customer"
    if unknown.any():
        log.append(("warn", "B1", f"{unknown.sum()} rows have no customer name or VAT number", df.loc[unknown, "source_row"].tolist()))
    spellings = df.dropna(subset=["Customer"]).groupby("customer_key")["Customer"].nunique()
    for key, n in spellings[spellings > 1].items():
        variants = sorted(df.loc[df["customer_key"] == key, "Customer"].dropna().unique())
        log.append(("info", "C2", f"{n} spellings merged into one customer (same VAT number): {', '.join(variants)}", []))

    df["month"] = df["issue_date"].dt.to_period("M")
    return df, log


# ---------------------------------------------------------------- bank

def classify_bank(raw, opening_balance):
    bank = raw.copy()
    bank["date"] = pd.to_datetime(bank["Date"])
    bank["category"] = "other"
    upper = bank["Description"].str.upper()
    for needle, cat in reversed(CATEGORY_RULES):        # reversed so the first rule wins
        bank.loc[upper.str.contains(needle, regex=False), "category"] = cat
    bank["month"] = bank["date"].dt.to_period("M")

    log = []
    # A3: balance continuity, opening + running sum must equal the printed balance
    expected = (opening_balance + bank["Amount"].cumsum()).round(2)
    breaks = (expected - bank["Balance"]).abs() > 0.05
    if breaks.any():
        log.append(("block", "A3", f"{breaks.sum()} statement lines where the balance doesn't add up", []))
    else:
        log.append(("info", "A3", "Statement balances add up on every line (opening + credits - debits = closing)", []))
    return bank, log


def match_payments(inv, bank):
    """Link customer-payment credits to invoices. Gives every invoice a paid date if we can."""
    inv = inv.copy()
    bank = bank.copy()
    bank["matched_invoices"] = ""
    live = inv[~inv["is_duplicate"] & ~inv["is_credit_note"]]
    cn = inv[inv["is_credit_note"] & ~inv["is_duplicate"]]
    cn_total = {}
    for _, r in cn.iterrows():
        m = re.search(r"INV-\d+", str(r.get("Notes", "")))
        if m:
            cn_total[m.group()] = cn_total.get(m.group(), 0) + r["gross"]
    expected_amount = {r["Invoice No"]: round(r["gross"] + cn_total.get(r["Invoice No"], 0), 2) for _, r in live.iterrows()}
    paid_on = {}

    pay = bank[bank["category"] == "customer_payment"]
    # 1) The description quotes invoice numbers (single or batched payments).
    for i, r in pay.iterrows():
        refs = re.findall(r"INV-\d+", r["Description"])
        refs = [x for x in refs if x in expected_amount]
        if refs:
            for x in refs:
                paid_on[x] = r["date"]
            bank.at[i, "matched_invoices"] = ",".join(refs)

    # 2) No reference: same amount, unpaid invoice issued 0-150 days before the payment.
    by_amount = {}
    for no, amt in expected_amount.items():
        by_amount.setdefault(amt, []).append(no)
    issue = live.set_index("Invoice No")["issue_date"].to_dict()
    for i, r in pay.iterrows():
        if bank.at[i, "matched_invoices"]:
            continue
        cands = [n for n in by_amount.get(round(r["Amount"], 2), [])
                 if n not in paid_on and 0 <= (r["date"] - issue[n]).days <= 150]
        if cands:
            n = min(cands, key=lambda x: issue[x])
            paid_on[n] = r["date"]
            bank.at[i, "matched_invoices"] = n

    inv["matched_paid_date"] = inv["Invoice No"].map(paid_on)
    inv["paid_date"] = inv["file_paid_date"].fillna(inv["matched_paid_date"])
    inv["paid_date_source"] = None
    inv.loc[inv["file_paid_date"].notna(), "paid_date_source"] = "file"
    inv.loc[inv["file_paid_date"].isna() & inv["matched_paid_date"].notna(), "paid_date_source"] = "matched"
    inv.loc[inv["is_credit_note"], "paid_date_source"] = "n/a"
    return inv, bank


# ---------------------------------------------------------------- metrics

def pmt(principal, annual_rate, months):
    r = annual_rate / 12
    return principal * r / (1 - (1 + r) ** -months)


def compute(inv, bank, company, amount=None, term=None):
    live = inv[~inv["is_duplicate"]]
    sales = live[~live["is_credit_note"]]
    months = pd.period_range(live["month"].min(), live["month"].max(), freq="M")

    m = pd.DataFrame(index=months)
    m["revenue_net"] = live.groupby("month")["net"].sum()
    m["credit_notes_net"] = live[live["is_credit_note"]].groupby("month")["net"].sum()
    m["gross_invoiced"] = live.groupby("month")["gross"].sum()
    credits = bank[bank["Amount"] > 0]
    m["customer_payments"] = credits[credits["category"] == "customer_payment"].groupby("month")["Amount"].sum()
    for cat in ["loan", "owner_injection", "own_transfer", "refund", "other"]:
        m[f"excluded_{cat}"] = credits[credits["category"] == cat].groupby("month")["Amount"].sum()
    debits = bank[bank["Amount"] < 0]
    m["operating_out"] = -debits[debits["category"].isin(OPERATING_OUT)].groupby("month")["Amount"].sum()
    m["debt_service"] = -debits[debits["category"] == "loan_repayment"].groupby("month")["Amount"].sum()
    m["invoices_count"] = sales.groupby("month").size()
    m = m.fillna(0)
    m["operating_cash_flow"] = m["customer_payments"] - m["operating_out"]

    # Payment lag: amount-weighted median days from issue to payment, so many small
    # cash sales don't hide how long the big customers take to pay.
    paid = sales.dropna(subset=["paid_date"])
    days = (paid["paid_date"] - paid["issue_date"]).dt.days
    order = days.sort_values().index
    cum = paid.loc[order, "gross"].cumsum() / paid["gross"].sum()
    lag_days = float(days.loc[cum[cum >= 0.5].index[0]])
    lag_m = int(round(lag_days / 30))
    days_to_paid = float((days * paid["gross"]).sum() / paid["gross"].sum())
    share_matched = float((sales["paid_date_source"] == "matched").sum() / max(1, sales["paid_date"].notna().sum()))

    # Reconciliation: invoices that should have been paid vs customer payments, shifted by the lag
    g, p = m["gross_invoiced"].values, m["customer_payments"].values
    n = len(m)
    inv_12, pay_12 = g[: n - lag_m].sum(), p[lag_m:].sum()
    gap_12m = (inv_12 - pay_12) / inv_12
    roll = []
    for end in range(2 + lag_m, n):
        i_win = g[end - 2 - lag_m: end - lag_m + 1].sum()
        p_win = p[end - 2: end + 1].sum()
        gap = (i_win - p_win) / i_win
        roll.append({"window": f"{months[end - 2]} to {months[end]}", "invoices_shifted": i_win,
                     "customer_payments": p_win, "gap": gap, "flagged": abs(gap) > GAP_ROLLING_AMBER})
    rolling = pd.DataFrame(roll)

    # Customers
    cust = live.groupby("customer").agg(revenue_net=("net", "sum"), invoices=("Invoice No", "count"))
    cust["share"] = cust["revenue_net"] / cust["revenue_net"].sum()
    cust = cust.sort_values("revenue_net", ascending=False)
    top5 = float(cust["share"].head(5).sum())
    largest = cust.index[0], float(cust["share"].iloc[0])

    # Receivables aging at the statement date
    as_of = bank["date"].max()
    cn_by_inv = {}
    for _, r in live[live["is_credit_note"]].iterrows():
        mm = re.search(r"INV-\d+", str(r.get("Notes", "")))
        if mm:
            cn_by_inv[mm.group()] = cn_by_inv.get(mm.group(), 0) + r["gross"]
    open_inv = sales[sales["paid_date"].isna()].copy()
    open_inv["outstanding"] = open_inv["gross"] + open_inv["Invoice No"].map(cn_by_inv).fillna(0)
    open_inv["days_past_due"] = (as_of - open_inv["due_date"]).dt.days
    buckets = pd.cut(open_inv["days_past_due"], [-10_000, 0, 30, 60, 90, 10_000],
                     labels=["Not yet due", "1-30 days", "31-60 days", "61-90 days", "90+ days"])
    aging = open_inv.groupby(buckets, observed=False)["outstanding"].sum().rename("outstanding").to_frame()
    overdue_by_cust = (open_inv[open_inv["days_past_due"] > OVERDUE_DAYS].groupby("customer")["outstanding"]
                       .sum().sort_values(ascending=False))

    # Repayment capacity
    amount = amount or company["request_amount_sar"]
    term = term or company["request_term_months"]
    new_installment = pmt(amount, PROFIT_RATE, term)
    avg_ocf = float(m["operating_cash_flow"].mean())
    current_debt = float(m["debt_service"].iloc[-3:].mean())
    dscr = avg_ocf / (current_debt + new_installment)

    first3, last3 = m["revenue_net"].iloc[:3].sum(), m["revenue_net"].iloc[-3:].sum()
    # What a naive check would say: net invoices vs every credit, same months, no lag.
    naive_gap = float((m["revenue_net"].sum() - credits["Amount"].sum()) / m["revenue_net"].sum())
    excluded_total = float(m[[c for c in m.columns if c.startswith("excluded_")]].sum().sum())

    metrics = {
        "as_of": as_of.date().isoformat(),
        "revenue_12m": float(m["revenue_net"].sum()),
        "revenue_growth": float((last3 - first3) / first3),
        "avg_monthly_revenue": float(m["revenue_net"].mean()),
        "payment_lag_days": lag_days, "payment_lag_months": lag_m,
        "days_to_get_paid": days_to_paid, "days_to_get_paid_estimated_share": share_matched,
        "gap_12m": float(gap_12m), "naive_gap": naive_gap,
        "rolling_flagged": int(rolling["flagged"].sum()) if len(rolling) else 0,
        "top5_share": top5, "largest_customer": largest[0], "largest_share": largest[1],
        "customers": int(len(cust)),
        "receivables_open": float(open_inv["outstanding"].sum()),
        "receivables_overdue_60": float(overdue_by_cust.sum()),
        "overdue_by_customer": overdue_by_cust.to_dict(),
        "excluded_credits": excluded_total,
        "loan_received": float(m["excluded_loan"].sum()),
        "avg_operating_cash_flow": avg_ocf,
        "cash_margin": float(m["operating_cash_flow"].sum() / m["revenue_net"].sum()),
        "peak_month": str(m["revenue_net"].idxmax()),
        "current_debt_service": current_debt,
        "request_amount": amount, "request_term": term,
        "new_installment": new_installment, "dscr": dscr,
        "coverage_months": int((m["invoices_count"] > 0).sum()),
    }
    return m, rolling, cust, aging, metrics


def build_flags(mt):
    f = []

    def add(level, title, detail):
        f.append({"level": level, "title": title, "detail": detail})

    if abs(mt["gap_12m"]) > GAP_12M_RED:
        add("red", "Invoices don't match bank receipts", f"12-month gap is {mt['gap_12m']:.1%} (threshold {GAP_12M_RED:.0%}).")
    else:
        add("green", "Invoices match bank receipts",
            f"12-month gap {mt['gap_12m']:.1%} after excluding non-sales credits and allowing a {mt['payment_lag_days']:.0f}-day payment lag.")
    if mt["rolling_flagged"]:
        add("amber", "Some 3-month windows don't reconcile", f"{mt['rolling_flagged']} rolling window(s) have a gap above {GAP_ROLLING_AMBER:.0%}.")

    for cust, amt in mt["overdue_by_customer"].items():
        if amt >= OVERDUE_CUSTOMER_RED:
            add("red", f"{cust} has stopped paying",
                f"SAR {amt:,.0f} is more than {OVERDUE_DAYS} days overdue. Ask the SME whether this will be recovered.")

    if mt["loan_received"] > 0:
        add("amber", "Existing financing found in the bank statements",
            f"SAR {mt['loan_received']:,.0f} disbursed, currently ~SAR {mt['current_debt_service']:,.0f}/month in installments. Confirm it is disclosed.")

    if mt["dscr"] < DSCR_RED:
        add("red", "Requested amount looks unaffordable",
            f"Cash flow covers debt payments {mt['dscr']:.2f}x (needs at least {DSCR_AMBER}x). Consider a smaller amount or longer term.")
    elif mt["dscr"] < DSCR_AMBER:
        add("amber", "Repayment capacity is tight", f"Cash flow covers debt payments {mt['dscr']:.2f}x (comfortable is {DSCR_AMBER}x+).")
    else:
        add("green", "Repayment capacity is comfortable", f"Cash flow covers debt payments {mt['dscr']:.2f}x.")

    if mt["largest_share"] > LARGEST_CUSTOMER_AMBER:
        add("amber", "High dependence on one customer", f"{mt['largest_customer']} is {mt['largest_share']:.0%} of revenue.")
    else:
        add("green", "No single customer dominates",
            f"Largest customer {mt['largest_customer']} is {mt['largest_share']:.0%}; top 5 are {mt['top5_share']:.0%}.")

    add("green" if mt["revenue_growth"] >= -0.10 else "amber", "Revenue trend",
        f"Last 3 months vs first 3 months: {mt['revenue_growth']:+.1%}. Seasonal business (Ramadan peak), so read with the monthly chart.")
    add("green" if mt["coverage_months"] == 12 else "red", "Data coverage", f"{mt['coverage_months']} of 12 months covered.")
    order = {"red": 0, "amber": 1, "green": 2}
    return sorted(f, key=lambda x: order[x["level"]])


def run(invoices_path, bank_path, company_path, amount=None, term=None):
    company = json.load(open(company_path))
    inv, inv_log = clean_invoices(pd.read_excel(invoices_path))
    bank, bank_log = classify_bank(pd.read_csv(bank_path), company["opening_balance"])
    inv, bank = match_payments(inv, bank)
    monthly, rolling, customers, aging, metrics = compute(inv, bank, company, amount, term)
    return Results(company, inv, bank, monthly, rolling, customers, aging, metrics,
                   build_flags(metrics), inv_log + bank_log)
