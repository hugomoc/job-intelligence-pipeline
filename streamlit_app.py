from __future__ import annotations

from datetime import date

import streamlit as st

from src.repositories.recommendation_repository import (
    load_all_jobs,
    update_application_status,
)
from src.resume.extractor import ResumeExtractionError
from src.ui.daily_workflow_service import (
    DailyWorkflowError,
    load_scoring_backlog_status,
    run_description_enrichment,
    run_email_ingestion,
    run_unscored_job_backlog,
)
from src.ui.job_recommendation_service import (
    JobRecommendationServiceError,
    process_resume_upload,
)


STATUS_LABELS = {
    "apply": "APPLY",
    "review": "REVIEW",
    "skip": "SKIP",
}

APPLICATION_STATUS_LABELS = {
    "new": "Not applied",
    "applied": "Applied",
    "removed": "Removed",
}


def format_seen_date(value: object) -> str | None:
    if value is None:
        return None

    if hasattr(value, "date"):
        return str(value.date())

    value_text = str(value).strip()

    if not value_text:
        return None

    return value_text.split()[0]


def to_date(value: object) -> date | None:
    if value is None:
        return None

    if hasattr(value, "date"):
        return value.date()

    value_text = str(value).strip()

    if not value_text:
        return None

    try:
        return date.fromisoformat(
            value_text.split()[0]
        )
    except ValueError:
        return None


def get_city(location: str | None) -> str:
    """Extract a city-like value from a job location."""

    if not location:
        return "Unknown"

    location = location.strip()

    if not location:
        return "Unknown"

    if location.casefold() == "remote":
        return "Remote"

    # Example: "San Diego, CA" becomes "San Diego"
    return location.split(",", maxsplit=1)[0].strip()


def render_list(title: str, items: list[str]) -> None:
    if not items:
        st.caption("None listed")
        return

    for item in items:
        st.markdown(f"- {item}")


def render_run_log(
    title: str,
    log_lines: tuple[str, ...],
) -> None:
    if not log_lines:
        return

    with st.expander(title):
        st.code("\n".join(log_lines), language="text")


def render_scoring_backlog_status(
    placeholder,
    resume_hash: str | None,
    minimum_rule_score: int,
):
    backlog_status = load_scoring_backlog_status(
        resume_hash=resume_hash,
        minimum_rule_score=minimum_rule_score,
    )

    with placeholder.container():
        if backlog_status.resume_hash is None:
            st.caption(
                "Upload a resume once before AI scoring backlog "
                "counts are available."
            )
            return backlog_status

        st.metric(
            "Still without AI score",
            backlog_status.unscored_candidates,
        )
        st.caption(
            "Already scored for this resume: "
            f"{backlog_status.cached_scores}. "
            "Already-scored jobs will not be sent again."
        )

    return backlog_status


def remove_generic_incomplete_risks(
    risks: list[str],
) -> list[str]:
    return [
        risk
        for risk in risks
        if "incomplete job description" not in risk.casefold()
    ]


def render_job_listing(job: dict) -> None:
    record_key = job["record_key"]
    application_status = job.get(
        "application_status",
        "new",
    )

    with st.container(border=True):
        header_left, header_right = st.columns([4, 1])

        with header_left:
            st.subheader(job["title"])
            st.caption(
                " | ".join(
                    value
                    for value in [
                        job.get("company_name"),
                        job.get("location"),
                        job.get("salary_text"),
                        job.get("source"),
                    ]
                    if value
                )
            )

        with header_right:
            if job.get("ai_score") is not None:
                recommendation = str(
                    job.get("recommendation") or ""
                ).lower()
                st.metric(
                    STATUS_LABELS.get(
                        recommendation,
                        "AI",
                    ),
                    f"{job['ai_score']}%",
                )
                st.caption(
                    f"Confidence: {job.get('confidence')}"
                )
            else:
                st.caption("Not AI-scored")

            st.caption(
                APPLICATION_STATUS_LABELS.get(
                    application_status,
                    application_status,
                )
            )

            st.link_button("Open job", job["apply_url"])

            if application_status == "applied":
                if st.button(
                    "Mark not applied",
                    key=f"unapply-{record_key}",
                ):
                    update_application_status(
                        record_key=record_key,
                        status="new",
                    )
                    st.rerun()
            elif application_status == "removed":
                if st.button(
                    "Restore",
                    key=f"restore-{record_key}",
                ):
                    update_application_status(
                        record_key=record_key,
                        status="new",
                    )
                    st.rerun()
            else:
                if st.button(
                    "Mark applied",
                    key=f"apply-{record_key}",
                    type="primary",
                ):
                    update_application_status(
                        record_key=record_key,
                        status="applied",
                    )
                    st.rerun()

                if st.button(
                    "Mark removed",
                    key=f"remove-{record_key}",
                ):
                    update_application_status(
                        record_key=record_key,
                        status="removed",
                    )
                    st.rerun()

        if job.get("posted_age_text"):
            st.caption(f"Posted: {job['posted_age_text']}")

        sent_date = format_seen_date(job.get("sent_at"))

        if sent_date:
            st.caption(f"Email date: {sent_date}")

        if job.get("best_search_title"):
            st.caption(
                "Best search: "
                f"{job['best_search_title']} "
                f"({job.get('rule_score') or 0})"
            )

        if job.get("has_incomplete_description"):
            description_word_count = job.get(
                "description_word_count"
            )
            if description_word_count is not None:
                st.warning(
                    "Incomplete job description "
                    f"({description_word_count} words)."
                )
            else:
                st.warning("Incomplete job description.")

        if job.get("ai_score") is not None:
            score_cols = st.columns(6)
            score_cols[0].metric("Title", job["title_fit"])
            score_cols[1].metric("Skills", job["skills_fit"])
            score_cols[2].metric("Experience", job["experience_fit"])
            score_cols[3].metric("Seniority", job["seniority_fit"])
            score_cols[4].metric("Industry", job["industry_fit"])
            score_cols[5].metric("Location", job["location_fit"])

            if job.get("summary"):
                st.markdown(job["summary"])

            detail_tabs = st.tabs(
                ["Strengths", "Missing", "Risks"]
            )

            with detail_tabs[0]:
                render_list(
                    "Strengths",
                    job["matching_strengths"],
                )

            with detail_tabs[1]:
                missing_items = (
                    job["hard_requirements_missing"]
                    + job["preferred_qualifications_missing"]
                )
                render_list(
                    "Missing qualifications",
                    missing_items,
                )

            with detail_tabs[2]:
                render_list(
                    "Risks",
                    remove_generic_incomplete_risks(
                        job["risk_factors"]
                    ),
                )

st.set_page_config(
    page_title="Job Intelligence",
    page_icon=None,
    layout="wide",
)

st.title("Job Intelligence")

if "show_saved_jobs" not in st.session_state:
    st.session_state["show_saved_jobs"] = False

with st.sidebar:
    st.header("Scoring")
    daily_minimum_rule_score = st.number_input(
        "Minimum rule score",
        min_value=0,
        max_value=100,
        value=0,
        step=5,
    )
    enrichment_limit = st.number_input(
        "Descriptions to enrich",
        min_value=1,
        max_value=100,
        value=20,
        step=5,
    )
    backlog_status_placeholder = st.empty()
    backlog_status = render_scoring_backlog_status(
        placeholder=backlog_status_placeholder,
        resume_hash=st.session_state.get("resume_hash"),
        minimum_rule_score=int(daily_minimum_rule_score),
    )
    score_limit_choice = st.selectbox(
        "Unscored jobs to process",
        options=[
            "20",
            "50",
            "100",
            "All unscored",
        ],
        help=(
            "Batch size for this run. Already-scored jobs are "
            "skipped automatically."
        ),
    )
    daily_score_limit = (
        max(backlog_status.unscored_candidates, 1)
        if score_limit_choice == "All unscored"
        else int(score_limit_choice)
    )

run_ingestion_clicked = st.sidebar.button(
    "Ingest emails",
)
score_backlog_clicked = st.sidebar.button(
    "Score unscored jobs",
)
enrich_descriptions_clicked = st.sidebar.button(
    "Enrich descriptions",
)
run_daily_clicked = st.sidebar.button(
    "Ingest, enrich and score",
    type="primary",
)
show_saved_jobs_clicked = st.sidebar.button(
    "Show saved jobs",
)
clear_view_clicked = st.sidebar.button(
    "Clear view",
)

if clear_view_clicked:
    st.session_state["show_saved_jobs"] = False
    st.session_state.pop("resume_hash", None)
    st.rerun()

if show_saved_jobs_clicked:
    st.session_state["show_saved_jobs"] = True

uploaded_file = st.file_uploader(
    "Upload a resume",
    type=["pdf", "docx"],
    accept_multiple_files=False,
)

if uploaded_file and st.button("Score jobs", type="primary"):
    try:
        with st.status("Processing resume...", expanded=True) as status:
            result = process_resume_upload(
                uploaded_file=uploaded_file,
                limit=int(daily_score_limit),
                minimum_rule_score=int(daily_minimum_rule_score),
            )

            st.write(f"Words extracted: {result.word_count}")
            st.write(
                "Resume profile: "
                + ("cached" if result.profile_was_cached else "created")
            )
            st.write(f"Already cached job scores: {result.cached_score_count}")
            st.write(f"New job scores saved: {result.successful_score_count}")
            st.write(f"Gemini scores saved: {result.gemini_score_count}")
            st.write(f"OpenAI scores saved: {result.openai_score_count}")
            st.write(f"Jobs that could not be scored: {result.failed_score_count}")
            st.write("dbt build: " + ("run" if result.dbt_was_run else "not needed"))
            status.update(label="Processing complete", state="complete")

        st.session_state["resume_hash"] = result.resume_hash
        st.session_state["show_saved_jobs"] = True
        render_scoring_backlog_status(
            placeholder=backlog_status_placeholder,
            resume_hash=st.session_state.get("resume_hash"),
            minimum_rule_score=int(daily_minimum_rule_score),
        )

    except ResumeExtractionError:
        st.error("The resume could not be read. Please upload a readable PDF or DOCX.")

    except JobRecommendationServiceError:
        st.error("The scoring workflow could not be completed. Please try again later.")

    except Exception:
        st.error("Something went wrong while processing the resume.")

ingestion_succeeded = True
enrichment_succeeded = True

if run_ingestion_clicked or run_daily_clicked:
    ingestion_succeeded = False

    try:
        with st.status("Reading email folders...", expanded=True) as status:
            ingestion_result = run_email_ingestion()
            st.write(f"New jobs inserted: {ingestion_result.inserted_jobs}")
            st.write(f"Duplicate jobs skipped: {ingestion_result.duplicate_jobs}")
            st.write(f"Previously processed emails skipped: {ingestion_result.skipped_emails}")
            st.write(f"Rule matches refreshed: {ingestion_result.rule_matches}")
            st.write(f"Processed emails recorded: {ingestion_result.processed_emails}")
            st.write(f"Total jobs stored: {ingestion_result.total_jobs}")
            render_run_log(
                "Email ingestion log",
                ingestion_result.log_lines,
            )
            status.update(label="Email ingestion complete", state="complete")
            ingestion_succeeded = True
            st.session_state["show_saved_jobs"] = True
            render_scoring_backlog_status(
                placeholder=backlog_status_placeholder,
                resume_hash=st.session_state.get("resume_hash"),
                minimum_rule_score=int(daily_minimum_rule_score),
            )

    except DailyWorkflowError:
        st.error("Email ingestion could not be completed. Please try again later.")

if enrich_descriptions_clicked or (run_daily_clicked and ingestion_succeeded):
    enrichment_succeeded = False

    try:
        with st.status("Visiting job pages...", expanded=True) as status:
            enrichment_result = run_description_enrichment(
                limit=int(enrichment_limit),
                minimum_words=80,
            )
            st.write(f"Jobs selected: {enrichment_result.jobs_selected}")
            st.write(f"Descriptions updated: {enrichment_result.descriptions_updated}")
            st.write(f"Pages blocked: {enrichment_result.blocked}")
            st.write(f"No description found: {enrichment_result.no_description}")
            st.write(f"Fetch errors: {enrichment_result.fetch_error}")
            st.write(f"Not improved: {enrichment_result.not_improved}")
            st.write(f"Rule matches refreshed: {enrichment_result.rule_matches_refreshed}")
            render_run_log(
                "Description enrichment log",
                enrichment_result.log_lines,
            )
            status.update(
                label="Description enrichment complete",
                state="complete",
            )
            enrichment_succeeded = True
            st.session_state["show_saved_jobs"] = True
            render_scoring_backlog_status(
                placeholder=backlog_status_placeholder,
                resume_hash=st.session_state.get("resume_hash"),
                minimum_rule_score=int(daily_minimum_rule_score),
            )

    except DailyWorkflowError:
        st.error("Job descriptions could not be enriched. Please try again later.")

if score_backlog_clicked or (
    run_daily_clicked
    and ingestion_succeeded
    and enrichment_succeeded
):
    try:
        with st.status("Scoring unscored jobs...", expanded=True) as status:
            backlog_result = run_unscored_job_backlog(
                resume_hash=st.session_state.get("resume_hash"),
                limit=int(daily_score_limit),
                minimum_rule_score=int(daily_minimum_rule_score),
            )
            st.session_state["resume_hash"] = backlog_result.resume_hash
            st.write(f"Already cached job scores: {backlog_result.already_scored}")
            st.write(f"Jobs selected for scoring: {backlog_result.candidates_selected}")
            st.write(f"New job scores saved: {backlog_result.scores_saved}")
            st.write(f"Gemini scores saved: {backlog_result.gemini_scores_saved}")
            st.write(f"OpenAI scores saved: {backlog_result.openai_scores_saved}")
            st.write(f"Jobs that could not be scored: {backlog_result.jobs_failed}")
            st.write("dbt build: " + ("run" if backlog_result.dbt_was_run else "not needed"))
            render_run_log(
                "AI scoring log",
                getattr(backlog_result, "log_lines", ()),
            )
            status.update(label="Scoring complete", state="complete")
            st.session_state["show_saved_jobs"] = True
            render_scoring_backlog_status(
                placeholder=backlog_status_placeholder,
                resume_hash=st.session_state.get("resume_hash"),
                minimum_rule_score=int(daily_minimum_rule_score),
            )

    except DailyWorkflowError:
        st.error("Unscored jobs could not be scored. Please try again later.")

jobs_tab, operations_tab = st.tabs(
    [
        "Jobs",
        "Operations",
    ]
)

with jobs_tab:
    st.subheader("Jobs")

    if not st.session_state["show_saved_jobs"]:
        st.info(
            "Start with the sidebar daily run controls, upload a resume, "
            "or choose Show saved jobs."
        )
        all_jobs = []
    else:
        all_jobs = load_all_jobs(
            resume_hash=st.session_state.get("resume_hash")
        )

    job_dates = [
        job_date
        for job_date in (
            to_date(job.get("sent_at"))
            for job in all_jobs
        )
        if job_date is not None
    ]

    filter_cols = st.columns([2, 2, 2, 1])

    with filter_cols[0]:
        source_filter = st.selectbox(
            "Source",
            options=[
                "All",
                *sorted(
                    {
                        job["source"]
                        for job in all_jobs
                    }
                ),
            ],
        )

    with filter_cols[1]:
        city_filter = st.selectbox(
            "City",
            options=[
                "All",
                *sorted(
                    {
                        get_city(job.get("location"))
                        for job in all_jobs
                    }
                ),
            ],
            key="all-jobs-city",
        )

    if job_dates:
        min_job_date = min(job_dates)
        max_job_date = max(job_dates)
    else:
        min_job_date = date.today()
        max_job_date = date.today()

    with filter_cols[2]:
        selected_dates = st.date_input(
            "Email date",
            value=(min_job_date, max_job_date),
            min_value=min_job_date,
            max_value=max_job_date,
        )

    with filter_cols[3]:
        scored_filter = st.selectbox(
            "AI score",
            options=[
                "All",
                "Scored",
                "Not scored",
            ],
        )

    secondary_filter_cols = st.columns([2, 2])

    with secondary_filter_cols[0]:
        recommendation_filter = st.selectbox(
            "Recommendation",
            options=[
                "All",
                "APPLY",
                "REVIEW",
                "SKIP",
                "Not scored",
            ],
        )

    with secondary_filter_cols[1]:
        applied_filter = st.selectbox(
            "Application status",
            options=[
                "Active",
                "All",
                "Not applied",
                "Applied",
                "Removed",
            ],
        )

    if isinstance(selected_dates, tuple):
        start_date, end_date = selected_dates
    else:
        start_date = selected_dates
        end_date = selected_dates

    filtered_jobs = all_jobs

    if source_filter != "All":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job["source"] == source_filter
        ]

    if city_filter != "All":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if get_city(job.get("location")) == city_filter
        ]

    if start_date and end_date:
        filtered_jobs = [
            job
            for job in filtered_jobs
            if (
                to_date(job.get("sent_at")) is not None
                and start_date
                <= to_date(job.get("sent_at"))
                <= end_date
            )
        ]

    if applied_filter == "Active":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job.get("application_status", "new")
            != "removed"
        ]
    elif applied_filter != "All":
        expected_status = {
            "Not applied": "new",
            "Applied": "applied",
            "Removed": "removed",
        }[applied_filter]
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job.get("application_status", "new")
            == expected_status
        ]

    if scored_filter == "Scored":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job.get("ai_score") is not None
        ]
    elif scored_filter == "Not scored":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job.get("ai_score") is None
        ]

    if recommendation_filter == "Not scored":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job.get("ai_score") is None
        ]
    elif recommendation_filter != "All":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if STATUS_LABELS.get(
                str(job.get("recommendation") or "").lower()
            )
            == recommendation_filter
        ]

    applied_count = sum(
        1
        for job in all_jobs
        if job.get("application_status") == "applied"
    )
    removed_count = sum(
        1
        for job in all_jobs
        if job.get("application_status") == "removed"
    )
    ai_scored_count = sum(
        1
        for job in all_jobs
        if job.get("ai_score") is not None
    )

    st.caption(
        f"Showing {len(filtered_jobs)} of {len(all_jobs)} "
        f"ingested jobs. AI-scored: {ai_scored_count}. "
        f"Applied: {applied_count}. Removed: {removed_count}"
    )

    if not filtered_jobs:
        st.info("No jobs match the selected filters.")

    for job in filtered_jobs:
        render_job_listing(job)

with operations_tab:
    current_jobs = (
        load_all_jobs(
            resume_hash=st.session_state.get("resume_hash")
        )
        if st.session_state["show_saved_jobs"]
        else []
    )
    total_jobs = len(current_jobs)
    ai_scored_jobs = sum(
        1
        for job in current_jobs
        if job.get("ai_score") is not None
    )
    unscored_jobs = total_jobs - ai_scored_jobs
    applied_jobs = sum(
        1
        for job in current_jobs
        if job.get("application_status") == "applied"
    )
    removed_jobs = sum(
        1
        for job in current_jobs
        if job.get("application_status") == "removed"
    )

    st.subheader("Pipeline Status")

    metric_cols = st.columns(5)
    metric_cols[0].metric("Stored jobs", total_jobs)
    metric_cols[1].metric("AI-scored", ai_scored_jobs)
    metric_cols[2].metric("Unscored", unscored_jobs)
    metric_cols[3].metric("Applied", applied_jobs)
    metric_cols[4].metric("Removed", removed_jobs)

    st.caption(
        "Use the sidebar daily run controls to ingest email folders "
        "and score only jobs that do not already have a current AI score."
    )
