"""Shared, provider-independent v9 score and recommendation decision.

The model proposes a fit score and a requirement ledger. This module never
trusts the score on its own: it reads the *validated* ledger (see
``evaluation.v9_evidence.validate_ledger``) and

* caps the score where the model's own verified evidence contradicts it
  (the "92% while missing a central mandatory qualification" failure), and
* flags scores far *below* what the verified evidence supports, so
  misleadingly low scores reach a human instead of being silently skipped.

The recommendation is one of APPLY, HUMAN REVIEW, LOW PRIORITY, SKIP and
is fully determined by the rules below, so both providers are judged by the
same policy. Every adjustment is reported in ``reasons``; nothing is hidden.

Decision truth table (single source of truth; the constants below, this
table, V9_GEMINI_EXPERIMENT.md and test_v9_scoring.py must stay in sync).

Score bands (established policy, applied to the capped final score):

    86-100  APPLY
    75-85   HUMAN REVIEW
    65-74   LOW PRIORITY
    <65     SKIP

Evidence caps on the model's score (strongest applicable cap wins):

    2+ central mandatory groups not demonstrated  -> cap 64 (SKIP band)
    1 central mandatory group not demonstrated    -> cap 74 (never HUMAN
                                                     REVIEW/APPLY band)
    central mandatory partial or unclear          -> cap 85 (never APPLY band)
    unverifiable citations / mislabels /
    inconsistent groups (integrity failures)      -> cap 85 (never APPLY band)
    peripheral mandatory group not demonstrated   -> cap 89

Recommendation overrides, in priority order (first match wins); the final
score itself is never changed by an override:

    1. human-confirmed eligibility blocker        -> final SKIP (overlay on
                                                     the technical result)
    2. 2+ central mandatory gaps                  -> SKIP
    3. 1 central mandatory gap                    -> LOW PRIORITY
                                                     (SKIP if final < 65)
    4. integrity failures                         -> HUMAN REVIEW
    5. no verified central mandatory extracted    -> HUMAN REVIEW
    6. all verified mandatory met but model
       score < 75 (under-scoring)                 -> HUMAN REVIEW, never a
                                                     silent LOW PRIORITY/SKIP
    7. otherwise: the score band, except that partial/unclear central
       evidence and unconfirmed JD eligibility turn a would-be APPLY or
       HUMAN REVIEW band into HUMAN REVIEW (they can never produce APPLY).

Missing preferred qualifications never trigger a cap or an override.

Isolated: no imports from production scoring, DuckDB, or provider SDKs.
"""
from __future__ import annotations

from evaluation.v9_evidence import Ledger, validate_ledger

APPLY = "APPLY"
HUMAN_REVIEW = "HUMAN REVIEW"
LOW_PRIORITY = "LOW PRIORITY"
SKIP = "SKIP"
RECOMMENDATIONS = (APPLY, HUMAN_REVIEW, LOW_PRIORITY, SKIP)

# Established recommendation bands: 86+ APPLY, 75-85 HUMAN REVIEW,
# 65-74 LOW PRIORITY, below 65 SKIP (see the module truth table).
APPLY_MIN = 86
REVIEW_MIN = 75
LOW_PRIORITY_MIN = 65

# Score ceilings implied by verified evidence about *central mandatory*
# requirement groups (and peripheral mandatory gaps), aligned to the bands.
CAP_ONE_CENTRAL_GAP = REVIEW_MIN - 1       # 74: never HUMAN REVIEW/APPLY band
CAP_TWO_CENTRAL_GAPS = LOW_PRIORITY_MIN - 1  # 64: SKIP band
CAP_CENTRAL_PARTIAL = APPLY_MIN - 1        # 85: related/insufficient: never APPLY band
CAP_CENTRAL_UNCLEAR = APPLY_MIN - 1        # 85: uncertain evidence: never APPLY band
CAP_PERIPHERAL_GAP = 89
CAP_INTEGRITY_FAILURE = APPLY_MIN - 1      # 85: invented citations: never APPLY band

# Below this, a model score contradicts a ledger where every verified
# mandatory group is met: flag as too low rather than skipping silently.
UNDER_SCORE_THRESHOLD = REVIEW_MIN


def decide(
    ledger: Ledger | dict,
    jd: str,
    resume: str,
    *,
    eligibility_blockers: list[str] | None = None,
    eligibility_confirmed: bool = False,
) -> dict:
    """Return the validated evidence plus final score and recommendation.

    ``eligibility_blockers`` are *candidate-confirmed* failures of a JD
    eligibility requirement (e.g. a clearance the candidate cannot obtain),
    supplied by the human, never inferred by a model. ``eligibility_confirmed``
    means the human confirmed every JD eligibility requirement is satisfied,
    which resolves eligibility rows the resume cannot prove.
    """
    obj = ledger if isinstance(ledger, Ledger) else Ledger.model_validate(ledger)
    validation = validate_ledger(obj, jd, resume)
    model_score = obj.fit_score
    reasons: list[str] = []
    caps: list[tuple[int, str]] = []

    # Requirements whose JD quote cannot be found are not counted as gaps:
    # an invented requirement must not push a score down either.
    verified = [g for g in validation["groups"] if g["jd_verified"]]
    mandatory = [g for g in verified if g["kind"] == "mandatory"]
    central = [g for g in mandatory if g["materiality"] == "central"]
    peripheral = [g for g in mandatory if g["materiality"] == "peripheral"]

    def by_status(groups, status):
        return [g["group_id"] for g in groups if g["status"] == status]

    central_gaps = by_status(central, "not_demonstrated")
    central_partial = by_status(central, "partial")
    central_unclear = by_status(central, "unclear")
    peripheral_gaps = by_status(peripheral, "not_demonstrated")
    integrity_rows = [r for r in validation["rows"] if r["integrity"]]
    structural = [
        f for f in validation["failures"]
        if f.startswith("group ")
        and any(marker in f for marker in ("inconsistent", "repeated component", " requires "))
    ]
    eligibility_open = [
        g["group_id"] for g in verified
        if g["kind"] == "eligibility" and g["status"] != "met"
    ]

    if len(central_gaps) >= 2:
        caps.append((CAP_TWO_CENTRAL_GAPS, f"central mandatory gaps: {', '.join(central_gaps)}"))
    elif central_gaps:
        caps.append((CAP_ONE_CENTRAL_GAP, f"central mandatory gap: {central_gaps[0]}"))
    if central_partial:
        caps.append((CAP_CENTRAL_PARTIAL, f"central mandatory only partially evidenced: {', '.join(central_partial)}"))
    if central_unclear:
        caps.append((CAP_CENTRAL_UNCLEAR, f"central mandatory evidence unclear: {', '.join(central_unclear)}"))
    if peripheral_gaps:
        caps.append((CAP_PERIPHERAL_GAP, f"peripheral mandatory gap: {', '.join(peripheral_gaps)}"))
    if integrity_rows or structural:
        caps.append((CAP_INTEGRITY_FAILURE, f"{len(integrity_rows)} row(s) with unverifiable citations or labels"))

    final_score = model_score
    for cap, why in sorted(caps):
        if final_score > cap:
            reasons.append(f"score capped {final_score}->{cap}: {why}")
            final_score = cap

    under_scored = (
        not integrity_rows and not structural and bool(central)
        and all(g["status"] == "met" for g in mandatory)
        and model_score < UNDER_SCORE_THRESHOLD
    )

    if eligibility_blockers:
        recommendation = SKIP
        reasons.append("confirmed eligibility blocker: " + "; ".join(eligibility_blockers))
    elif len(central_gaps) >= 2:
        recommendation = SKIP
        reasons.append("two or more central mandatory requirements not demonstrated")
    elif central_gaps:
        recommendation = SKIP if final_score < LOW_PRIORITY_MIN else LOW_PRIORITY
        reasons.append("a central mandatory requirement is not demonstrated")
    elif integrity_rows or structural:
        recommendation = HUMAN_REVIEW
        reasons.append("ledger cites text not found in the JD/resume or mislabels requirements")
    elif not central:
        recommendation = HUMAN_REVIEW
        reasons.append("no verified central mandatory requirements extracted; ledger likely incomplete")
    elif under_scored:
        recommendation = HUMAN_REVIEW
        reasons.append(
            f"model score {model_score} is lower than its verified evidence supports "
            "(all mandatory requirements met)")
    elif final_score < LOW_PRIORITY_MIN:
        recommendation = SKIP
    elif final_score < REVIEW_MIN:
        recommendation = LOW_PRIORITY
    elif central_partial or central_unclear:
        recommendation = HUMAN_REVIEW
        reasons.append("central mandatory evidence is partial or unclear")
    elif eligibility_open and not eligibility_confirmed:
        recommendation = HUMAN_REVIEW
        reasons.append("JD eligibility requirement unconfirmed: " + ", ".join(eligibility_open))
    elif final_score >= APPLY_MIN:
        recommendation = APPLY
    else:
        recommendation = HUMAN_REVIEW

    return {
        "model_score": model_score,
        "final_score": final_score,
        "recommendation": recommendation,
        "reasons": reasons,
        "score_adjusted": final_score != model_score,
        "under_scored": under_scored,
        "central_mandatory": {
            "total": len(central),
            "met": len(by_status(central, "met")),
            "partial": len(central_partial),
            "unclear": len(central_unclear),
            "not_demonstrated": len(central_gaps),
        },
        "integrity_failure_rows": len(integrity_rows),
        "unsupported_resume_citations": sum(not r["resume_verified"] for r in validation["rows"]),
        "unsupported_jd_citations": sum(not r["jd_verified"] for r in validation["rows"]),
        "validation": validation,
    }
