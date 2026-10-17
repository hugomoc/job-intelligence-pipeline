"""Pure stored-result evaluation and aggregate-only report rendering."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict

from src.verified_posting_identity import normalize_official_url, verified_posting_key_from_url

VERIFIED = frozenset({"FOUND_VERIFIED", "VERIFIED"})
DEFERRED_NOT_FOUND = frozenset({"OFFICIAL_NOT_FOUND", "NOT_FOUND", "SEARCH_DEFERRED", "DEFERRED"})
UNRESOLVED = DEFERRED_NOT_FOUND | {"AMBIGUOUS"}
DECISIONS = ("apply", "review", "skip")


def is_verified(row: dict) -> bool:
    return str(row.get("official_url_status") or "").upper() in VERIFIED and bool(row.get("official_job_url"))


def equivalent_urls(first: str, second: str) -> bool:
    if not first or not second:
        return False
    try:
        first_key, second_key = verified_posting_key_from_url(first), verified_posting_key_from_url(second)
        if first_key and second_key:
            return first_key == second_key
        return normalize_official_url(first) == normalize_official_url(second)
    except ValueError:
        return False


def wilson_interval(hits: int, n: int, z: float = 1.959963984540054) -> tuple[float, float] | None:
    if n < 0 or not 0 <= hits <= n:
        raise ValueError("Proportion counts must satisfy 0 <= hits <= n")
    if not n:
        return None
    p = hits / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def metric(value: float | None, n: int) -> dict:
    return {"value": value, "n": n, "status": "insufficient data" if n < 10 or value is None else "ok"}


def proportion(hits: int, n: int) -> dict:
    return {**metric(hits / n if n else None, n), "hits": hits, "wilson_95": wilson_interval(hits, n)}


def resolution_metrics(rows: list[dict]) -> dict:
    verified = [row for row in rows if is_verified(row)]
    decided = [row for row in verified if row.get("resolution_label") in {"correct", "wrong_job"}]
    expected = [row for row in rows if row.get("expected_official_url")]
    return {
        "precision": proportion(sum(row["resolution_label"] == "correct" for row in decided), len(decided)),
        "false_accept_rate": proportion(sum(row["resolution_label"] == "wrong_job" for row in decided), len(decided)),
        "recall": proportion(sum(is_verified(row) and equivalent_urls(
            row.get("official_job_url", ""), row["expected_official_url"]
        ) for row in expected), len(expected)),
        "deferred_not_found_rate": proportion(sum(
            str(row.get("official_url_status") or "").upper() in DEFERRED_NOT_FOUND for row in rows
        ), len(rows)),
        "unsure_count": sum(row.get("resolution_label") == "unsure" for row in rows),
        "verified_unsure_count": sum(row.get("resolution_label") == "unsure" for row in verified),
        "verified_count": len(verified), "n": len(rows),
    }


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        for index in order[start:end]:
            ranks[index] = (start + end + 1) / 2
        start = end
    return ranks


def spearman(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second):
        raise ValueError("Rank inputs must have equal lengths")
    if len(first) < 2:
        return None
    x, y = _average_ranks(first), _average_ranks(second)
    x_mean, y_mean = sum(x) / len(x), sum(y) / len(y)
    covariance = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    variance = sum((a - x_mean) ** 2 for a in x) * sum((b - y_mean) ** 2 for b in y)
    return max(-1.0, min(1.0, covariance / math.sqrt(variance))) if variance else None


def scoring_metrics(rows: list[dict]) -> dict:
    paired = [row for row in rows if row.get("overall_score") is not None
              and row.get("my_fit_rating") not in (None, "")]
    scores = [float(row["overall_score"]) for row in paired]
    ratings = [float(row["my_fit_rating"]) for row in paired]
    errors = [abs(score - rating) for score, rating in zip(scores, ratings)]
    decisions = [row for row in rows if row.get("recommendation") and row.get("my_decision") in DECISIONS]
    matrix = {stored: {human: 0 for human in DECISIONS} for stored in (*DECISIONS, "other")}
    by_version = defaultdict(list)
    for row in decisions:
        stored = row["recommendation"] if row["recommendation"] in DECISIONS else "other"
        matrix[stored][row["my_decision"]] += 1
        by_version[str(row.get("prompt_version") or "missing")].append(row)
    return {
        "mae": metric(sum(errors) / len(errors) if errors else None, len(errors)),
        "spearman": metric(spearman(scores, ratings), len(paired)),
        "within_10": proportion(sum(error <= 10 for error in errors), len(errors)),
        "agreement": proportion(sum(row["recommendation"] == row["my_decision"] for row in decisions), len(decisions)),
        "confusion_matrix": {"n": len(decisions), "status": "insufficient data" if len(decisions) < 10 else "ok", "counts": matrix},
        "agreement_by_prompt_version": {version: proportion(sum(
            row["recommendation"] == row["my_decision"] for row in cohort
        ), len(cohort)) for version, cohort in sorted(by_version.items())},
        "n": len(rows),
    }


def _display(value: dict, *, percent: bool = False) -> str:
    if value["status"] != "ok":
        return f"insufficient data (n={value['n']})"
    text = f"{100 * value['value']:.1f}%" if percent else f"{value['value']:.3f}"
    if "wilson_95" in value:
        low, high = value["wilson_95"]
        text += f"; 95% Wilson CI [{100 * low:.1f}%, {100 * high:.1f}%]"
    return f"{text} (n={value['n']})"


def _version_label(version: str) -> str:
    # Do not echo arbitrary stored strings into the aggregate public report.
    return version if re.fullmatch(r"v\d+(?:[._-]\d+)*", version) else "version-" + hashlib.sha256(version.encode()).hexdigest()[:12]


def render_report(rows: list[dict], *, label_count: int | None = None, unmatched_count: int = 0) -> str:
    resolution, scoring = resolution_metrics(rows), scoring_metrics(rows)
    counts = Counter(str(row.get("official_url_status") or "missing").upper() for row in rows)
    lines = ["# Stored-result evaluation (offline)", "", "Aggregate counts and metrics only; no per-job identifiers.", "",
             f"Label rows: {label_count if label_count is not None else len(rows)}; joined rows: {len(rows)}; unmatched/excluded rows: {unmatched_count}.",
             f"Stored-verified rows: {resolution['verified_count']}; unsure labels: {resolution['unsure_count']} (verified: {resolution['verified_unsure_count']}); ambiguous rows: {counts['AMBIGUOUS']}.",
             f"Stored scores with incomplete descriptions: {sum(row.get('overall_score') is not None and row.get('description_complete') is False for row in rows)}.", "",
             "## Resolution", "", "| Metric | Result |", "| --- | --- |"]
    for name in ("precision", "false_accept_rate", "recall", "deferred_not_found_rate"):
        lines.append(f"| {name} | {_display(resolution[name], percent=True)} |")
    lines += ["", "Unsure and blank resolution labels are excluded from precision and false-accept denominators. Recall uses only rows with a supplied expected URL and requires a stored verified result. Deferred/not-found rate uses all joined label rows; AMBIGUOUS is counted separately.",
              "", "URL comparison: src.verified_posting_identity.verified_posting_key_from_url, with normalize_official_url as the fallback (tracking removed; identity parameters retained).", "",
              "## Fit scoring", "", "| Metric | Result |", "| --- | --- |"]
    for name in ("mae", "spearman", "within_10", "agreement"):
        lines.append(f"| {name} | {_display(scoring[name], percent=name in {'within_10', 'agreement'})} |")
    matrix = scoring["confusion_matrix"]
    lines += ["", f"Confusion matrix (stored rows, human columns; n={matrix['n']}; {matrix['status']}):", "",
              "| Stored recommendation | apply | review | skip |", "| --- | ---: | ---: | ---: |"]
    for stored, cells in matrix["counts"].items():
        lines.append(f"| {stored} | {cells['apply']} | {cells['review']} | {cells['skip']} |")
    lines += ["", "Agreement by stored prompt version (nonstandard version names are hashed):", "",
              "| Prompt version | Agreement |", "| --- | --- |"]
    for version, result in scoring["agreement_by_prompt_version"].items():
        lines.append(f"| {_version_label(version)} | {_display(result, percent=True)} |")
    lines += ["", "MAE, rank correlation and within-10 use paired numeric ratings only. Decision agreement uses paired decisions only. Spearman uses average ranks for ties; constant ranks are undefined. Estimates with n < 10, or undefined correlation, are reported as insufficient data.",
              "", "This is a stratified labeled sample, not a population-wide recall estimate. Stored results can be stale, incomplete or from different resume profiles; the latest score per exact record key is used without rescoring. Wilson intervals describe the selected labeled rows and do not correct selection bias. No persisted per-job search/AI request counts were found in the source schemas; cost metrics are omitted.", ""]
    return "\n".join(lines)
