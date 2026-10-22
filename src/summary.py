"""The credit summary at the top of the dashboard.

Two ways to write it:
  1. rule_based_summary(): always available, deterministic, built from the metrics.
  2. ai_summary(): optional. Sends only the computed metrics and flags (no invoices,
     no customer data beyond names in flags) to Claude and asks for a short memo.
     Used only when the `anthropic` package is installed and credentials are set
     (e.g. ANTHROPIC_API_KEY). Any failure falls back to the rule-based text.
"""

import json
import os

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are helping a bank credit analyst in Saudi Arabia review an SME \
financing application. You receive metrics already computed from the SME's invoices \
and bank statements, plus automated flags. Write a short credit memo:

1. One sentence on the business and the request.
2. Strengths (2-3 bullets) and risks (2-3 bullets), each citing a number from the data.
3. Questions the analyst should ask the SME before deciding (2-3 bullets).
4. A suggested next step (e.g. proceed, proceed with conditions, request more information).

Only use numbers present in the data. Do not invent facts. Do not make the final credit \
decision; the analyst decides. Use SAR and plain English. Keep it under 220 words."""


def _payload(company, metrics, flags):
    keep = ["revenue_12m", "revenue_growth", "avg_monthly_revenue", "payment_lag_days", "days_to_get_paid",
            "gap_12m", "rolling_flagged", "top5_share", "largest_customer", "largest_share", "customers",
            "receivables_open", "receivables_overdue_60", "excluded_credits", "loan_received",
            "avg_operating_cash_flow", "current_debt_service", "request_amount", "request_term",
            "new_installment", "dscr", "coverage_months"]
    return {
        "company": {k: company[k] for k in ("legal_name", "city", "founded")},
        "request": {"amount_sar": metrics["request_amount"], "term_months": metrics["request_term"],
                    "purpose": company["request_purpose"]},
        "metrics": {k: round(metrics[k], 4) if isinstance(metrics[k], float) else metrics[k] for k in keep},
        "flags": flags,
    }


def rule_based_summary(company, m, flags):
    reds = [f for f in flags if f["level"] == "red"]
    ambers = [f for f in flags if f["level"] == "amber"]
    if reds:
        step = "Request more information before deciding: " + "; ".join(f["title"].lower() for f in reds) + "."
    elif ambers:
        step = "Proceed with conditions, after clarifying: " + "; ".join(f["title"].lower() for f in ambers) + "."
    else:
        step = "Proceed to standard credit approval."
    return (
        f"**{company['legal_name']}** is requesting **SAR {m['request_amount']:,.0f} over {m['request_term']} months** "
        f"(purpose: {company['request_purpose'][0].lower() + company['request_purpose'][1:]}). Over the last 12 months it invoiced **SAR {m['revenue_12m']:,.0f}** "
        f"net of VAT across {m['customers']} customers, and those invoices are backed by bank receipts "
        f"(12-month gap {m['gap_12m']:.1%}). Customers take about {m['days_to_get_paid']:.0f} days to pay. "
        f"The largest customer is {m['largest_share']:.0%} of revenue. At the requested terms, operating cash flow "
        f"covers all debt payments **{m['dscr']:.2f}x**.\n\n"
        f"**{len(reds)} red and {len(ambers)} amber flags.** {step}"
    )


def ai_available():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def ai_summary(company, metrics, flags):
    """Returns (text, error). On any problem, text is None and error explains why."""
    try:
        import anthropic
    except ImportError:
        return None, "The anthropic package is not installed."
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=2000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(_payload(company, metrics, flags), ensure_ascii=False)}],
        )
    except anthropic.APIConnectionError:
        return None, "Could not reach the Claude API."
    except anthropic.RateLimitError:
        return None, "Claude API rate limit reached; try again shortly."
    except anthropic.APIStatusError as e:
        return None, f"Claude API error ({e.status_code})."
    if response.stop_reason == "refusal":
        return None, "The model declined this request."
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return (text or None), (None if text else "Empty response.")
