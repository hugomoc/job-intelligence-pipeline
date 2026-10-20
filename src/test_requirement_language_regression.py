"""Regression checks for human-language eligibility and JD section headings."""
from src.ai.requirement_policy import (
    _required_language_names,
    description_requirement_clauses,
    personal_fact_kind,
)


def test_technical_proficiency_is_not_a_personal_language_requirement():
    for clause in (
        "Advanced proficiency in SQL for data modeling",
        "Proficient in Python and dbt",
        "Fluent in SQL Server",
        "Written communication skills with Python",
    ):
        assert personal_fact_kind(clause) != "language"
        assert not _required_language_names(clause)


def test_explicit_human_languages_still_detected():
    assert personal_fact_kind("Fluent in English and Portuguese") == "language"
    assert _required_language_names("Fluent in English and Portuguese") == {
        "english", "portuguese"
    }
    assert _required_language_names("French language required") == {"french"}


def test_core_requirements_are_mandatory_not_optional():
    jd = """The Toolkit: Technical skills vs. nice-to-haves
Core Requirements
Proficiency in programming languages including SQL, Python, and JavaScript/TypeScript.
Nice-to-Haves
Experience with Dagster preferred.
"""
    rows = description_requirement_clauses(jd)
    core = next(row for row in rows if "JavaScript" in row["clause"])
    assert core["section"] == "required"
    assert core["mandatory"] is True
    assert core["optional"] is False


def test_optional_heading_only_when_entire_heading_matches():
    rows = description_requirement_clauses(
        "Technical skills vs. nice-to-haves\nCore Requirements\nExpert SQL skills"
    )
    sql = next(row for row in rows if "Expert SQL" in row["clause"])
    assert sql["mandatory"] and not sql["optional"]


def test_optional_french_does_not_reclassify_mandatory_english():
    rows = description_requirement_clauses(
        "Required Qualifications\nFluent in English\n"
        "Preferred Qualifications\nFrench language preferred"
    )
    english = next(row for row in rows if "English" in row["clause"])
    french = next(row for row in rows if "French" in row["clause"])
    assert english["mandatory"] and not english["optional"]
    assert french["optional"] and not french["mandatory"]
