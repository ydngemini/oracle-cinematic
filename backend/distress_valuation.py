"""
Distress-aware valuation — the safety layer over the retail AVM.

Every AVM (RentCast, ATTOM, ...) returns RETAIL fair market value: it assumes
typical/good condition and an arms-length, on-market sale. Oracle targets the
opposite — off-market, distressed owners (probate, foreclosure, tax lien,
absentee) whose properties (a) often carry deferred maintenance an AVM cannot
see and (b) trade BELOW retail in private, as-is, quick-close deals. Anchoring an
acquisition offer on the retail AVM therefore systematically OVERPAYS on exactly
these properties.

This module converts a retail FMV into the decision-relevant figures, with every
assumption surfaced and nothing presented as observed fact:

  - ARV (after-repair / retail value) = the AVM number (worth fixed up, on-market)
  - As-is value range                 = ARV minus a private-sale liquidity discount
                                        and an unknown-condition repair reserve
                                        scaled by distress severity
  - Max Allowable Offer (MAO)         = the acquisition CEILING via the investor
                                        70% rule: 0.70 * ARV - estimated repairs

Because overpaying is the asymmetric risk (overpay → lose money; under-pay → just
lose the deal), the layer biases conservative and lowers confidence when interior
condition is unknown and distress is high. These are MODEL ESTIMATES pending an
interior inspection — never an appraisal.
"""

from dataclasses import dataclass, field

SEVERE = "SEVERE"
MODERATE = "MODERATE"
LOW = "LOW"
UNKNOWN = "UNKNOWN"

# Distress signals that imply likely significant repairs and/or a forced sale.
_SEVERE_SIGNALS = {
    "PRE_FORECLOSURE", "PREFORECLOSURE", "NOTICE_OF_DEFAULT", "NOD", "FORECLOSURE",
    "LIS_PENDENS", "AUCTION", "SHERIFF_SALE", "REO", "BANKRUPTCY",
    "TAX_LIEN", "TAX_DELINQUENT", "DELINQUENT_TAX", "CODE_VIOLATION", "CONDEMNED",
    "FIRE_DAMAGE", "WATER_DAMAGE", "STORM_DAMAGE", "HOARDER",
    "PROBATE", "ESTATE", "DECEASED", "INHERITED_VACANT", "VACANT",
}
# Distress with moderate condition/forced-sale risk.
_MODERATE_SIGNALS = {
    "TAX_EXEMPT", "DIVORCE", "DIVORCE_FILING", "INHERITED", "EXPIRED_LISTING",
    "EXPIRED", "RELOCATION", "JOB_LOSS", "DOWNSIZE", "SENIOR", "LIEN", "JUDGMENT",
    "DELINQUENT", "NOTICE",
}
# Motivation signals where condition is often fine (off-market, not damaged).
_LOW_SIGNALS = {
    "ABSENTEE_OWNER", "ABSENTEE", "HIGH_EQUITY", "LONG_OWNERSHIP", "TIRED_LANDLORD",
    "OUT_OF_STATE", "GENERAL", "MOTIVATED_SELLER", "EQUITY",
}

# Repair-reserve range (fraction of ARV) by severity — captures UNKNOWN interior
# condition typical of off-market acquisitions, since the AVM assumes good repair.
_RESERVE = {
    SEVERE:   (0.15, 0.30),
    MODERATE: (0.08, 0.18),
    LOW:      (0.03, 0.10),
    UNKNOWN:  (0.05, 0.15),
}
# Private / as-is / quick-close liquidity discount (fraction of ARV), independent
# of condition — the gap between an MLS-exposed retail sale and a private sale.
_LIQUIDITY = (0.05, 0.10)
# Never imply an absurd discount.
_MAX_TOTAL_DISCOUNT = 0.50
# Investor 70% rule: max acquisition = PROFIT_FACTOR * ARV - estimated repairs.
_PROFIT_FACTOR = 0.70


@dataclass
class DistressValuation:
    arv: int                      # retail / after-repair value (the AVM number)
    as_is_low: int
    as_is_mid: int
    as_is_high: int
    investor_offer: int           # MAO — acquisition ceiling
    severity: str
    signals: list = field(default_factory=list)
    repair_reserve_pct: float = 0.0     # mid repair reserve (fraction of ARV)
    liquidity_discount_pct: float = 0.0
    total_discount_pct: float = 0.0     # mid ARV→as_is discount
    confidence_penalty: float = 0.0     # subtract from the AVM confidence
    assumptions: list = field(default_factory=list)


def _norm(s) -> str:
    return str(s).strip().upper().replace(" ", "_").replace("-", "_") if s else ""


def gather_signals(subject: dict) -> list:
    """Collect distress signals from whatever the subject carries: the single
    `life_event` (graph hit), an optional `distress_flags` list, and a coarse
    `record_type`. Returns a sorted, de-duplicated, normalized list."""
    sigs = set()
    le = _norm(subject.get("life_event"))
    if le:
        sigs.add(le)
    for f in (subject.get("distress_flags") or []):
        nf = _norm(f)
        if nf:
            sigs.add(nf)
    return sorted(sigs)


def classify(signals, motivation: float = 0.0) -> str:
    """Severity tier from the signal set, nudged by the motivation score."""
    sset = set(signals)
    if any(s in _SEVERE_SIGNALS for s in sset):
        sev = SEVERE
    elif any(s in _MODERATE_SIGNALS for s in sset):
        sev = MODERATE
    elif any(s in _LOW_SIGNALS for s in sset):
        sev = LOW
    else:
        sev = UNKNOWN
    # A very high motivation score with only mild signals still implies pressure;
    # bump one tier rather than under-reserve.
    if sev in (LOW, UNKNOWN) and motivation >= 70:
        sev = MODERATE
    return sev


def adjust(arv, subject: dict, strategy: str = "wholesale") -> DistressValuation:
    """Convert a retail ARV into as-is range + a conservative max offer.

    `strategy`: "wholesale" → 70%-rule MAO (acquisition ceiling for resale);
                "standard"  → pay near as-is market (owner-occupant buyer)."""
    try:
        arv = int(arv or 0)
    except (TypeError, ValueError):
        arv = 0

    signals = gather_signals(subject)
    try:
        motivation = float(subject.get("novelty_score") or subject.get("motivation_score") or 0)
    except (TypeError, ValueError):
        motivation = 0.0
    severity = classify(signals, motivation)

    r_lo, r_hi = _RESERVE[severity]
    l_lo, l_hi = _LIQUIDITY
    # Discount range. The LARGER discount yields the LOWER as-is bound.
    d_small = min(l_lo + r_lo, _MAX_TOTAL_DISCOUNT)
    d_large = min(l_hi + r_hi, _MAX_TOTAL_DISCOUNT)
    reserve_mid = (r_lo + r_hi) / 2
    total_mid = (d_small + d_large) / 2

    base_assumptions = [
        "Retail/ARV from the AVM assumes typical/good condition and an arms-length, "
        "on-market sale.",
    ]
    penalty = 0.10 + (0.10 if severity == SEVERE else 0.05 if severity == MODERATE else 0.0)

    if arv <= 0:
        # Nothing to anchor on — return a neutral, all-zero result but keep the
        # severity assessment so the UI/contract still shows the risk profile.
        return DistressValuation(
            arv=0, as_is_low=0, as_is_mid=0, as_is_high=0, investor_offer=0,
            severity=severity, signals=signals,
            repair_reserve_pct=round(reserve_mid, 3),
            liquidity_discount_pct=round((l_lo + l_hi) / 2, 3),
            total_discount_pct=round(total_mid, 3),
            confidence_penalty=round(penalty, 2),
            assumptions=base_assumptions + ["No base value available — as-is/offer not computed."],
        )

    as_is_high = int(arv * (1 - d_small))
    as_is_low = int(arv * (1 - d_large))
    as_is_mid = (as_is_low + as_is_high) // 2
    repairs = int(arv * reserve_mid)

    if strategy == "wholesale":
        mao = max(int(_PROFIT_FACTOR * arv - repairs), 0)
        # Sanity: the acquisition ceiling is a discount to as-is, never above it.
        mao = min(mao, as_is_low)
        offer_note = (
            f"Max offer = 70% of ARV (${arv:,}) − estimated repairs (${repairs:,}) "
            f"= ${mao:,}. The 30% margin covers investor profit, holding, closing "
            f"and resale risk."
        )
    else:
        mao = as_is_mid
        offer_note = f"Standard purchase: offer near as-is market value (${as_is_mid:,})."

    assumptions = base_assumptions + [
        f"Distress severity {severity} from signals {signals or ['none']} → "
        f"{int(r_lo*100)}–{int(r_hi*100)}% repair reserve (interior condition unknown, off-market).",
        f"As-is range applies a {int(l_lo*100)}–{int(l_hi*100)}% private/as-is liquidity discount.",
        offer_note,
        "Model estimates pending interior inspection — not an appraisal.",
    ]

    return DistressValuation(
        arv=arv,
        as_is_low=as_is_low,
        as_is_mid=as_is_mid,
        as_is_high=as_is_high,
        investor_offer=mao,
        severity=severity,
        signals=signals,
        repair_reserve_pct=round(reserve_mid, 3),
        liquidity_discount_pct=round((l_lo + l_hi) / 2, 3),
        total_discount_pct=round(total_mid, 3),
        confidence_penalty=round(penalty, 2),
        assumptions=assumptions,
    )
