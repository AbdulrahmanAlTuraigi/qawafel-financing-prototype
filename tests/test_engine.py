"""Tests for the financing engine. Run: pytest"""

import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
import engine  # noqa: E402

DATA = os.path.join(ROOT, "data")


# ---------------------------------------------------------------- helpers

def invoices(*rows):
    """rows: (invoice_no, issue_date, gross). Builds the frame match_payments expects."""
    return pd.DataFrame([{
        "Invoice No": no, "issue_date": pd.Timestamp(d), "gross": g, "Notes": None,
        "is_duplicate": False, "is_credit_note": False, "file_paid_date": pd.NaT,
    } for no, d, g in rows])


def payments(*rows):
    """rows: (date, description, amount). All are customer payments."""
    return pd.DataFrame([{
        "date": pd.Timestamp(d), "Description": desc, "Amount": a, "category": "customer_payment",
    } for d, desc, a in rows])


def status(inv):
    return inv.set_index("Invoice No")[["paid_date", "received_from_bank", "outstanding"]]


# ---------------------------------------------------------------- payment matching

def test_partial_payment_is_not_marked_paid():
    inv, bank = engine.match_payments(
        invoices(("INV-1", "2026-01-01", 100_000)),
        payments(("2026-02-01", "TRF REF INV-1", 1_000)))
    s = status(inv).loc["INV-1"]
    assert pd.isna(s["paid_date"])
    assert s["received_from_bank"] == 1_000
    assert s["outstanding"] == 99_000


def test_partial_payments_add_up_to_paid():
    inv, _ = engine.match_payments(
        invoices(("INV-1", "2026-01-01", 10_000)),
        payments(("2026-02-01", "TRF REF INV-1", 4_000), ("2026-03-01", "TRF REF INV-1", 6_000)))
    s = status(inv).loc["INV-1"]
    assert s["paid_date"] == pd.Timestamp("2026-03-01")     # paid when the last part arrives
    assert s["outstanding"] == 0


def test_batch_payment_pays_oldest_first_and_keeps_the_rest_open():
    inv, bank = engine.match_payments(
        invoices(("INV-1", "2026-01-01", 5_000), ("INV-2", "2026-01-10", 5_000)),
        payments(("2026-02-15", "SADAD BATCH INV INV-2,INV-1", 7_000)))
    s = status(inv)
    assert s.loc["INV-1", "outstanding"] == 0
    assert s.loc["INV-2", "outstanding"] == 3_000
    assert bank.loc[0, "unallocated"] == 0


def test_overpayment_is_reported_not_dropped():
    inv, bank = engine.match_payments(
        invoices(("INV-1", "2026-01-01", 5_000)),
        payments(("2026-02-01", "TRF REF INV-1", 6_000)))
    assert status(inv).loc["INV-1", "outstanding"] == 0
    assert bank.loc[0, "allocated"] == 5_000
    assert bank.loc[0, "unallocated"] == 1_000


def test_amount_match_needs_exact_amount_and_time_window():
    inv, bank = engine.match_payments(
        invoices(("INV-1", "2026-01-01", 2_300), ("INV-2", "2025-01-01", 4_600)),
        payments(("2026-01-20", "CASH DEPOSIT", 2_300),        # exact amount, 19 days later: match
                 ("2026-01-21", "CASH DEPOSIT", 2_299),        # 1 SAR off: no match
                 ("2026-01-22", "CASH DEPOSIT", 4_600)))       # right amount, but over a year later: no match
    s = status(inv)
    assert s.loc["INV-1", "outstanding"] == 0
    assert s.loc["INV-2", "outstanding"] == 4_600
    assert list(bank["unallocated"]) == [0, 2_299, 4_600]


def test_never_allocates_more_than_was_received():
    r = engine.run(*_sample_paths())
    pay = r.bank[r.bank["category"] == "customer_payment"]
    assert (pay["allocated"] <= pay["Amount"] + 0.01).all()
    assert (r.invoices["received_from_bank"] <= r.invoices["expected_amount"].fillna(0) + 0.01).all()
    assert pay["allocated"].sum() + pay["unallocated"].sum() == pytest.approx(pay["Amount"].sum(), abs=1)


# ---------------------------------------------------------------- sample data vs ground truth

def _sample_paths():
    return (os.path.join(DATA, "invoices_2025-10_to_2026-09.xlsx"),
            os.path.join(DATA, "bank_transactions.csv"), os.path.join(DATA, "company.json"))


@pytest.fixture(scope="module")
def sample():
    return engine.run(*_sample_paths())


@pytest.fixture(scope="module")
def truth():
    return (pd.read_csv(os.path.join(DATA, "_ground_truth_invoices.csv")),
            pd.read_csv(os.path.join(DATA, "_ground_truth_bank.csv")))


def test_duplicates_are_removed(sample, truth):
    true_inv, _ = truth
    assert (~sample.invoices["is_duplicate"]).sum() == len(true_inv)


def test_text_amounts_are_converted(sample, truth):
    true_inv, _ = truth
    clean = sample.invoices[~sample.invoices["is_duplicate"]].set_index("Invoice No")["net"]
    expected = true_inv.set_index("Invoice No")["Net Amount (SAR)"]
    assert clean.loc[expected.index].round(2).equals(expected.round(2))


def test_customer_spellings_are_merged(sample, truth):
    true_inv, _ = truth
    assert sample.metrics["customers"] == true_inv["_true_customer"].nunique()


def test_bank_lines_are_classified_correctly(sample, truth):
    _, true_bank = truth
    got, want = sample.bank["category"], true_bank["_true_category"]
    # A supplier with no keyword in its name can only be flagged as "unclassified_out", never as income or a loan.
    ok = (got == want) | ((got == "unclassified_out") & want.isin(engine.OPERATING_OUT))
    assert ok.all(), sample.bank.loc[~ok, ["Description", "category"]]


def test_operating_costs_are_not_understated(sample, truth):
    _, true_bank = truth
    true_out = -true_bank.loc[true_bank["_true_category"].isin(engine.OPERATING_OUT), "Amount"].sum()
    assert sample.monthly["operating_out"].sum() == pytest.approx(true_out, abs=1)


def test_revenue_ties_back_to_invoice_rows(sample):
    live = sample.invoices[~sample.invoices["is_duplicate"]]
    assert sample.monthly["revenue_net"].sum() == pytest.approx(live["net"].sum(), abs=0.01)


# ---------------------------------------------------------------- repayment

def test_installment_formula():
    # 120,000 over 12 months at 10% a year: standard annuity payment.
    assert engine.pmt(120_000, 0.10, 12) == pytest.approx(10_549.91, abs=0.01)


def test_dscr_definition(sample):
    m = sample.metrics
    assert m["dscr"] == pytest.approx(m["avg_operating_cash_flow"] / (m["current_debt_service"] + m["new_installment"]))
    assert m["dscr_weakest_3m"] <= m["dscr"]


def test_longer_term_improves_coverage():
    short = engine.run(*_sample_paths(), amount=1_200_000, term=12).metrics["dscr"]
    long = engine.run(*_sample_paths(), amount=1_200_000, term=18).metrics["dscr"]
    assert long > short
