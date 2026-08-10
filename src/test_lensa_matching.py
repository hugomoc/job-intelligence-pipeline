from src.resolvers.job_matching import (
    score_candidate,
    select_best_candidate,
    score_company,
    score_title,
)


def test_title_tolerates_remote_suffix() -> None:
    assert (
        score_title(
            "Senior Data Engineer",
            "Senior Data Engineer - Remote",
        )
        >= 90
    )


def test_company_normalization_exact() -> None:
    assert score_company("General Motors", "General Motors ") == 100


def test_title_punctuation_normalization() -> None:
    assert (
        score_title(
            "Remote Staff TPM, Developer Experience",
            "Remote Staff TPM Developer Experience",
        )
        == 100
    )


def test_wrong_company_is_penalized() -> None:
    match = score_candidate(
        expected_title="Senior Data Engineer",
        expected_company="ICF",
        candidate_title="Senior Data Engineer",
        candidate_company="Random Company",
    )

    assert match.score < 75
    assert select_best_candidate([match]) is None


def test_exact_match_ranks_above_partial_match() -> None:
    exact = score_candidate(
        expected_title="Senior Data Engineer",
        expected_company="ICF",
        candidate_title="Senior Data Engineer",
        candidate_company="ICF",
    )
    partial = score_candidate(
        expected_title="Senior Data Engineer",
        expected_company="ICF",
        candidate_title="Senior Data Engineer II",
        candidate_company="ICF",
    )

    assert exact.score > partial.score
    assert select_best_candidate([partial, exact]) == exact


def test_ambiguous_high_scoring_candidates_are_rejected() -> None:
    first = score_candidate(
        expected_title="Senior Data Engineer Platform",
        expected_company="ICF",
        candidate_title="Senior Data Engineer Platform Lead",
        candidate_company="ICF",
    )
    second = score_candidate(
        expected_title="Senior Data Engineer Platform",
        expected_company="ICF",
        candidate_title="Senior Data Engineer Platform Manager",
        candidate_company="ICF",
    )

    assert select_best_candidate([first, second]) is None


def main() -> None:
    test_title_tolerates_remote_suffix()
    test_company_normalization_exact()
    test_title_punctuation_normalization()
    test_wrong_company_is_penalized()
    test_exact_match_ranks_above_partial_match()
    test_ambiguous_high_scoring_candidates_are_rejected()
    print("Lensa matching tests passed.")


if __name__ == "__main__":
    main()
