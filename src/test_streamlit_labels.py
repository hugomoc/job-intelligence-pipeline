from pathlib import Path


def test_title_classification_summary_wording() -> None:
    source = Path("streamlit_app.py").read_text()

    assert "Filtered by title:" not in source
    assert "Title-classified as filtered out:" in source



def compact_card_app(**overrides):
    """Render only the real card, with callbacks isolated from persistence/AI."""
    from streamlit.testing.v1 import AppTest

    job = dict(
        record_key="example-job", title="Senior Data Engineer",
        company_name="Example Company", location="Remote", source="linkedin",
        salary_text="$150K", ai_score=95, recommendation="apply",
        candidate_assessment_id="saved-assessment", application_status="new",
        discovered_at="2026-10-20", best_search_title="Data Engineer", rule_score=90,
        title_classification="STRONG_MATCH", title_match_score=95,
        title_filter_reason="Title matches", fit_priority_label="High",
        fit_priority_source="AI", description_state="FULL_JD",
        enrichment_status="enriched", identity_confidence=0.95,
        resolved_candidate_url="https://example.invalid/jobs/123",
        has_current_complete_ai_assessment=True,
        similar_job_previously_removed=True,
        actionability_reason="Trusted exact official posting key unavailable",
        summary="Saved assessment summary", matching_strengths=["Python"],
        hard_requirements_missing=["Example gap"], risk_factors=["Example risk"],
    )
    job.update(overrides)
    source = Path("streamlit_app.py").read_text().split("\nst.set_page_config(", 1)[0]
    source += """
def change_application_status(record_key, status):
    st.session_state['status_call'] = (record_key, status)
def manual_rescore(record_key):
    st.session_state['rescore_call'] = record_key
"""
    return AppTest.from_string(source + f"\nrender_job_listing({job!r})", default_timeout=30).run()


def test_compact_card_header_and_collapsed_details():
    app = compact_card_app()
    assert not app.exception
    badges = app.get("html")[0].proto.body
    assert 'aria-label="AI fit 95%"' in badges
    assert '>95%</span>' in badges and '>APPLY</span>' in badges
    assert [item.label for item in app.expander] == [
        "Scoring details", "Enrichment details", "Advanced actions",
    ]
    assert all(not item.proto.expanded for item in app.expander)
    assert [item.label for item in app.expander[2].button] == ["Rescore this job"]
    captions = [item.value for item in app.caption]
    metadata = next(text for text in captions if "Email date:" in text)
    assert all(label in metadata for label in (
        "Best search:", "Title Fit:", "Fit priority:", "Description:",
    ))
    assert any("Similar job previously removed" in text for text in captions)
    assert any("Current status unknown" in text for text in captions)
    assert any("Resolved page passed identity validation" in text for text in captions)
    assert any("Trusted exact official posting key unavailable" in text for text in captions)
    assert any("Saved assessment summary" in item.value for item in app.markdown)
    # Each primary action lives in a separate column of the header's action area.
    header = app.main[0].children[0]
    assert len(header.children) == 3
    action_row = header.children[2].children[0]
    assert len(action_row.children) == 3
    assert [child.type for child in action_row.children.values()] == ["column"] * 3
    assert app.get("link_button")[0].proto.url == "https://example.invalid/jobs/123"


def test_compact_card_callbacks_keep_existing_keys_and_arguments():
    for key, status in (("apply-example-job", "applied"), ("remove-example-job", "removed")):
        app = compact_card_app()
        app.button(key=key).click().run()
        assert not app.exception
        assert app.session_state["status_call"] == ("example-job", status)
    app = compact_card_app()
    app.button(key="rescore-example-job").click().run()
    assert not app.exception
    assert app.session_state["rescore_call"] == "example-job"


def test_compact_card_unscored_and_handled_states():
    app = compact_card_app(ai_score=None, candidate_assessment_id=None,
        application_status="removed", has_current_complete_ai_assessment=False)
    assert not app.exception
    assert not app.button
    assert "Scoring details" not in [item.label for item in app.expander]
    assert "Advanced actions" not in [item.label for item in app.expander]
    assert any(item.value == "Removed" for item in app.caption)
    badges = app.get("html")[0].proto.body
    assert '>Unscored</span>' in badges
    assert '%' not in badges and '>APPLY</span>' not in badges



def test_assessment_badge_colors_and_escaped_text():
    for recommendation, score, foreground in (
        ("apply", 98, "#86efac"), ("review", 75, "#fcd34d"), ("skip", 49, "#cbd5e1"),
    ):
        app = compact_card_app(ai_score=score, recommendation=recommendation)
        assert not app.exception
        badges = app.get("html")[0].proto.body
        assert f'>{score}%</span>' in badges
        assert f'>{recommendation.upper()}</span>' in badges
        assert f'color:{foreground}' in badges
        assert 'font-size:24px' in badges and 'border:1px solid' in badges
        assert '<style' not in badges
    app = compact_card_app(recommendation='<img src=x onerror="bad()">')
    assert not app.exception
    badges = app.get("html")[0].proto.body
    assert '<img' not in badges and '&lt;IMG' in badges


def main() -> None:
    test_title_classification_summary_wording()
    test_compact_card_header_and_collapsed_details()
    test_compact_card_callbacks_keep_existing_keys_and_arguments()
    test_compact_card_unscored_and_handled_states()
    test_assessment_badge_colors_and_escaped_text()
    print("Streamlit label tests passed.")


if __name__ == "__main__":
    main()
