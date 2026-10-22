"""Generate 12 months of made-up data for a fictional Saudi food wholesaler.

Outputs (in data/):
  invoices_2025-10_to_2026-09.xlsx   what the SME's accountant would upload
  bank_transactions.csv              what our statement parser would extract
  bank_statements/statement_YYYY-MM.pdf  the statement PDFs themselves

Everything is fictional. The seed is fixed so the data is reproducible.
Assumptions are listed in README.md and marked ASSUMPTION below.
"""

import os
import random
from datetime import date, timedelta

import pandas as pd

SEED = 42
random.seed(SEED)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

START = date(2025, 10, 1)
# Invoices from Aug-Sep 2025 are generated too (not uploaded), so customers'
# payments for them land in Oct-Nov, like a real business with open receivables.
GEN_START = date(2025, 8, 1)
END = date(2026, 9, 30)
VAT_RATE = 0.15

COMPANY = {
    "legal_name": "Sarab Al Riyadh Food Distribution Co.",
    "cr_number": "1010999123",
    "vat_number": "300999888700003",
    "city": "Riyadh",
    "sector": "Food and grocery wholesale",
    "founded": 2017,
    "bank_account": "SA** **** **** **** **** 4417",
}

REQUEST = {
    "amount_sar": 750_000,
    "term_months": 12,
    "purpose": "Working capital to stock up before Ramadan 2027 (stock purchases Dec 2026 to Jan 2027)",
}

# ASSUMPTION: seasonality for a Riyadh food wholesaler. Ramadan 2026 is roughly
# 18 Feb to 19 Mar, so shops stock up in January and February. Eid al-Adha is
# late May. Summer is slow (people travel), and back-to-school lifts August.
SEASONALITY = {
    (2025, 8): 0.90, (2025, 9): 0.95,
    (2025, 10): 0.95, (2025, 11): 1.00, (2025, 12): 1.00,
    (2026, 1): 1.15, (2026, 2): 1.35, (2026, 3): 1.20,
    (2026, 4): 0.85, (2026, 5): 1.05, (2026, 6): 0.90,
    (2026, 7): 0.80, (2026, 8): 0.95, (2026, 9): 1.00,
}
MONTHLY_GROWTH = 0.01           # ASSUMPTION: ~12% a year underlying growth
BASE_MONTHLY_NET = 880_000      # ASSUMPTION: ~SAR 11M a year net revenue

# name, segment, share of revenue, payment terms (days), behaviour, VAT number
CUSTOMERS = [
    ("Al Noor Hypermarket", "Hypermarket", 0.24, 60, "slow", "310100200300003"),
    ("Qimma Mini Markets", "Mini-market chain", 0.09, 30, "good", "310100200300013"),
    ("Bayt Al Mandi Restaurants", "Restaurant group", 0.07, 30, "good", "310100200300023"),
    ("Riyadh Grand Hotel Supplies", "Hotel", 0.06, 45, "good", "310100200300033"),
    ("Golden Spoon Restaurants", "Restaurant group", 0.05, 30, "default", "310100200300043"),
    ("Al Safwa Grocery", "Mini-market", 0.04, 30, "good", "310100200300053"),
    ("Najd Catering Services", "Catering", 0.04, 45, "late", "310100200300063"),
    ("Dar Al Qahwa Cafes", "Cafe chain", 0.035, 30, "good", "310100200300073"),
    ("Al Wadi Supermarket", "Supermarket", 0.035, 30, "good", "310100200300083"),
    ("Sunrise Bakery", "Bakery", 0.03, 15, "good", "310100200300093"),
]
SMALL_SHOPS = [
    "Baqala Al Hamra", "Baqala Al Malqa", "Al Yasmin Market", "Al Narjis Grocery",
    "Hittin Corner Store", "Al Aqiq Market", "Al Sahafa Baqala", "Tuwaiq Market",
    "Al Rabwa Grocery", "Al Muruj Market", "Diriyah Fresh", "Al Olaya Corner",
    "Al Wurud Baqala", "King Fahd Rd Market", "Al Suwaidi Grocery", "Al Shifa Market",
    "Al Aziziyah Baqala", "Namar Mini Mart", "Laban Grocery", "Al Rawdah Market",
    "Qurtubah Store", "Al Nadheem Market", "Ishbiliyah Baqala", "Al Hazm Grocery",
    "Dhahrat Laban Market", "Al Maather Store", "Al Wizarat Baqala", "Ghirnatah Market",
    "Al Nafel Grocery", "Al Taawun Mart",
]
# ASSUMPTION: small shops share the remaining ~31% and mostly pay cash or 15 days.
remaining = 1 - sum(c[2] for c in CUSTOMERS)
weights = [random.uniform(0.5, 1.5) for _ in SMALL_SHOPS]
for name, w in zip(SMALL_SHOPS, weights):
    terms = random.choice([0, 0, 0, 15, 15, 30])
    CUSTOMERS.append((name, "Small shop", remaining * w / sum(weights), terms,
                      "good", None if random.random() < 0.4 else f"3101009{random.randint(10000000, 99999999)}3"))

# Messiness: the biggest customer appears under three spellings (risk C5).
NAME_VARIANTS = {"Al Noor Hypermarket": ["Al Noor Hypermarket", "AlNoor Hyper Mkt", "النور هايبرماركت"]}

ORDERS_PER_MONTH = {
    "Hypermarket": 8, "Mini-market chain": 6, "Restaurant group": 6, "Hotel": 4,
    "Mini-market": 4, "Catering": 4, "Cafe chain": 5, "Supermarket": 4,
    "Bakery": 8, "Small shop": 3,
}


def months(start=START):
    y, m = start.year, start.month
    while date(y, m, 1) <= END:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def month_end(y, m):
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return nxt - timedelta(days=1)


def payment_delay(terms, behaviour, issue):
    """Days between issue and payment. None = never paid (as of END)."""
    if behaviour == "default" and issue >= date(2026, 4, 15):
        return None                                  # stopped paying mid-April
    if terms == 0:
        return 0
    if behaviour == "slow":
        return terms + random.randint(5, 25)
    if behaviour == "late":
        return terms + random.randint(20, 55)
    return max(0, terms + random.randint(-5, 10))


def build_invoices():
    rows, seq, cn_seq = [], 10001, 501
    for idx, (y, m) in enumerate(months(GEN_START)):
        target = BASE_MONTHLY_NET * SEASONALITY[(y, m)] * (1 + MONTHLY_GROWTH) ** idx
        last = month_end(y, m)
        for name, segment, share, terms, behaviour, vat in CUSTOMERS:
            n = max(1, round(ORDERS_PER_MONTH[segment] * random.uniform(0.6, 1.3)))
            cust_target = target * share * random.uniform(0.85, 1.15)
            for _ in range(n):
                issue = date(y, m, random.randint(1, last.day))
                if behaviour == "default" and issue > date(2026, 7, 31):
                    continue                          # stopped ordering after July
                net = round(cust_target / n * random.uniform(0.6, 1.4), 2)
                vat_amt = round(net * VAT_RATE, 2)
                delay = payment_delay(terms, behaviour, issue)
                paid = issue + timedelta(days=delay) if delay is not None else None
                if paid and paid > END:
                    paid = None                       # not yet paid at statement date
                shown_name = random.choice(NAME_VARIANTS.get(name, [name]))
                rows.append({
                    "Invoice No": f"INV-{seq}", "Invoice Date": issue,
                    "Due Date": issue + timedelta(days=terms),
                    "Customer": shown_name, "Customer VAT No": vat,
                    "Type": "Invoice", "Net Amount (SAR)": net, "VAT (SAR)": vat_amt,
                    "Total (SAR)": round(net + vat_amt, 2), "Paid Date": paid,
                    "_true_customer": name, "_terms": terms,
                })
                seq += 1
                # ASSUMPTION: ~3% of invoices get a partial return (credit note).
                if random.random() < 0.03:
                    pct = random.uniform(0.05, 0.3)
                    cn_date = issue + timedelta(days=random.randint(3, 20))
                    if cn_date <= END:
                        cn_net = -round(net * pct, 2)
                        rows.append({
                            "Invoice No": f"CN-{cn_seq}", "Invoice Date": cn_date,
                            "Due Date": None, "Customer": shown_name, "Customer VAT No": vat,
                            "Type": "Credit Note", "Net Amount (SAR)": cn_net,
                            "VAT (SAR)": round(cn_net * VAT_RATE, 2),
                            "Total (SAR)": round(cn_net * (1 + VAT_RATE), 2), "Paid Date": None,
                            "Notes": f"Return against INV-{seq - 1}",
                            "_true_customer": name, "_terms": terms,
                        })
                        cn_seq += 1
    df = pd.DataFrame(rows).sort_values("Invoice Date").reset_index(drop=True)
    return df


def add_messiness(df):
    """Realistic problems a parser must handle. Each is documented in README."""
    df = df.copy()
    df["Notes"] = df.get("Notes")
    rng = random.Random(SEED + 1)
    inv_idx = df.index[df["Type"] == "Invoice"].tolist()

    # 1. ~30% of paid dates were never filled in by the accountant (risk B3).
    for i in inv_idx:
        if pd.notna(df.at[i, "Paid Date"]) and rng.random() < 0.30:
            df.at[i, "Paid Date"] = None

    # 2. Three invoices pasted twice (risk C1).
    dups = df.loc[rng.sample(inv_idx, 3)].copy()
    dups["Notes"] = None

    # 3. Two rows with no customer name (risk B2), and five amounts typed as text (risk A3).
    for i in rng.sample(inv_idx, 2):
        df.at[i, "Customer"] = None
    df["Net Amount (SAR)"] = df["Net Amount (SAR)"].astype(object)
    for i in rng.sample(inv_idx, 5):
        df.at[i, "Net Amount (SAR)"] = f"SAR {df.at[i, 'Net Amount (SAR)']:,.2f}"

    df = pd.concat([df, dups]).sort_values("Invoice Date").reset_index(drop=True)
    return df


def build_bank(df_true):
    """Bank transactions consistent with the (clean) invoices."""
    rng = random.Random(SEED + 2)
    tx = []

    def add(d, desc, amount, true_cat):
        tx.append({"Date": d, "Description": desc, "Amount": round(amount, 2), "_true_category": true_cat})

    inv = df_true[df_true["Type"] == "Invoice"]
    cn = df_true[df_true["Type"] == "Credit Note"]
    cn_by_inv = {r["Notes"].split()[-1]: r["Total (SAR)"] for _, r in cn.iterrows()}

    # Customer payments
    big = inv[inv["_true_customer"] == "Al Noor Hypermarket"]
    for _, r in inv.drop(big.index).iterrows():
        if pd.isna(r["Paid Date"]):
            continue
        amount = r["Total (SAR)"] + cn_by_inv.get(r["Invoice No"], 0)
        if r["_terms"] == 0:
            add(r["Paid Date"], "CASH DEPOSIT - RIYADH BRANCH", amount, "customer_payment")
        elif rng.random() < 0.6:
            add(r["Paid Date"], f"INCOMING TRF {r['_true_customer'].upper()} REF {r['Invoice No']}", amount, "customer_payment")
        else:
            add(r["Paid Date"], f"INCOMING TRF {r['_true_customer'].upper()}", amount, "customer_payment")

    # The hypermarket pays in weekly batches listing invoice numbers.
    big_paid = big.dropna(subset=["Paid Date"]).copy()
    big_paid["week"] = big_paid["Paid Date"].apply(lambda d: d - timedelta(days=d.weekday()) + timedelta(days=3))
    for wk, g in big_paid.groupby("week"):
        if wk > END:
            wk = END
        amount = sum(r["Total (SAR)"] + cn_by_inv.get(r["Invoice No"], 0) for _, r in g.iterrows())
        refs = ",".join(g["Invoice No"])
        add(wk, f"SADAD BATCH AL NOOR HYPERMARKET INV {refs}", amount, "customer_payment")

    # Non-sales credits (risk C7)
    add(date(2026, 1, 12), "FINANCE DISBURSEMENT - WORKING CAPITAL FACILITY", 400_000, "loan")
    add(date(2026, 7, 6), "TRF FROM KHALID A. (OWNER) PERSONAL ACC", 150_000, "owner_injection")
    for d in [date(2025, 12, 28), date(2026, 3, 2), date(2026, 8, 30)]:
        add(d, "TRF FROM OWN ACC ****7782", rng.choice([80_000, 120_000, 100_000]), "own_transfer")
    add(date(2026, 5, 18), "SUPPLIER REFUND - DAMAGED GOODS", 12_400, "refund")

    # Debits
    inv_dates = pd.to_datetime(inv["Invoice Date"])
    def net_rev(y, m):
        return inv[(inv_dates.dt.year == y) & (inv_dates.dt.month == m)]["Net Amount (SAR)"].sum()

    prev_net = net_rev(2025, 9)          # September purchases are paid in October
    for y, m in months():
        # ASSUMPTION: goods cost ~80% of net sales (thin margins are normal in food wholesale). Suppliers give ~30 days, so last
        # month's purchases are paid this month (plus 15% input VAT), to ~6 suppliers.
        cogs_gross = prev_net * 0.73 * (1 + VAT_RATE)
        for k in range(6):
            add(date(y, m, rng.randint(1, 28)), f"OUTGOING TRF {['DAIRY SUPPLIER A', 'RICE IMPORTER B', 'COOKING OIL SUPPLIER C', 'POULTRY SUPPLIER D', 'SPICES SUPPLIER E', 'PACKAGING SUPPLIER F'][k]}", -cogs_gross / 6 * rng.uniform(0.85, 1.15), "supplier")
        prev_net = net_rev(y, m)
        add(date(y, m, 27), "PAYROLL - WPS SALARIES", -92_000, "payroll")
        add(date(y, m, 15), "GOSI CONTRIBUTIONS", -11_500, "payroll")
        add(date(y, m, 10), "SEC ELECTRICITY + STC TELECOM", -rng.uniform(7_000, 14_000), "utilities")
        if m in (1, 4, 7, 10):
            add(date(y, m, 5), "WAREHOUSE RENT - QUARTERLY", -75_000, "rent")
            # Net VAT for the previous quarter = 15% of (sales - purchases) ~ 15% x 20% of net sales.
            add(date(y, m, 28), "ZATCA VAT PAYMENT", -prev_net * 3 * VAT_RATE * 0.20, "tax")
        if date(y, m, 1) >= date(2026, 2, 1):
            add(date(y, m, 12), "FINANCE INSTALLMENT - WORKING CAPITAL FACILITY", -18_500, "loan_repayment")

    bank = pd.DataFrame(tx).sort_values(["Date", "Amount"], ascending=[True, False]).reset_index(drop=True)
    bank = bank[(bank["Date"] >= START) & (bank["Date"] <= END)].reset_index(drop=True)
    opening = 480_000.00
    bank["Balance"] = (opening + bank["Amount"].cumsum()).round(2)
    return bank, opening


def write_statement_pdfs(bank, opening, folder):
    """One PDF per month, like a bank's e-statement."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    os.makedirs(folder, exist_ok=True)
    styles = getSampleStyleSheet()
    bal = opening
    for y, m in months():
        mb = bank[(pd.to_datetime(bank["Date"]).dt.year == y) & (pd.to_datetime(bank["Date"]).dt.month == m)]
        close = mb["Balance"].iloc[-1] if len(mb) else bal
        doc = SimpleDocTemplate(os.path.join(folder, f"statement_{y}-{m:02d}.pdf"), pagesize=A4)
        rows = [["Date", "Description", "Debit", "Credit", "Balance"]]
        for _, r in mb.iterrows():
            rows.append([str(r["Date"]), r["Description"][:60],
                         f"{-r['Amount']:,.2f}" if r["Amount"] < 0 else "",
                         f"{r['Amount']:,.2f}" if r["Amount"] > 0 else "", f"{r['Balance']:,.2f}"])
        t = Table(rows, colWidths=[60, 250, 65, 65, 75], repeatRows=1)
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 6.5),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("ALIGN", (2, 0), (-1, -1), "RIGHT")]))
        doc.build([
            Paragraph("Example Bank (fictional) - Account statement", styles["Title"]),
            Paragraph(f"{COMPANY['legal_name']} | Account {COMPANY['bank_account']} | {y}-{m:02d}", styles["Normal"]),
            Paragraph(f"Opening balance: SAR {bal:,.2f} | Closing balance: SAR {close:,.2f}", styles["Normal"]),
            Spacer(1, 8), t,
        ])
        bal = close


def main():
    os.makedirs(DATA, exist_ok=True)
    all_invoices = build_invoices()
    bank, opening = build_bank(all_invoices)
    clean = all_invoices[pd.to_datetime(all_invoices["Invoice Date"]) >= pd.Timestamp(START)].reset_index(drop=True)
    messy = add_messiness(clean)

    upload = messy.drop(columns=["_true_customer", "_terms"])
    upload.to_excel(os.path.join(DATA, "invoices_2025-10_to_2026-09.xlsx"), index=False, sheet_name="Invoices")
    bank.drop(columns=["_true_category"]).to_csv(os.path.join(DATA, "bank_transactions.csv"), index=False)
    write_statement_pdfs(bank, opening, os.path.join(DATA, "bank_statements"))

    # Ground truth, kept separately so the engine can be tested against it.
    clean.to_csv(os.path.join(DATA, "_ground_truth_invoices.csv"), index=False)
    bank.to_csv(os.path.join(DATA, "_ground_truth_bank.csv"), index=False)
    pd.Series({**COMPANY, **{f"request_{k}": v for k, v in REQUEST.items()}, "opening_balance": opening}).to_json(
        os.path.join(DATA, "company.json"), indent=2)
    print(f"{len(upload)} invoice rows, {len(bank)} bank transactions written to {DATA}")


if __name__ == "__main__":
    main()
