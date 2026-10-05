"""Streamlit entrypoint for the daily job-intelligence workflow.

This file should stay presentation-focused: collect user input, call service
functions, and render results. Database queries, email ingestion, enrichment,
and AI scoring live in src/ so they can be tested without Streamlit.
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from src.enrich_jobs import DEFAULT_ENRICHMENT_LIMIT
from src.repositories.recommendation_repository import (
    derive_fit_priority,
    load_all_jobs,
    load_pipeline_operation_metrics,
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
from src.ui.job_links import select_job_open_target
from src.ui.job_pipeline_state import derive_job_pipeline_state
from src.ui.pagination import PAGE_SIZE_OPTIONS, paginate_items


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

TITLE_FIT_LABELS = {
    "STRONG_MATCH": "Strong",
    "POSSIBLE_MATCH": "Possible",
    "FILTERED_OUT": "Filtered out",
}

DESCRIPTION_STATE_LABELS = {
    "FULL_JD": "Full JD",
    "PARTIAL_JD": "Partial JD",
    "NEEDS_ENRICHMENT": "Needs enrichment",
    "ENRICHMENT_REJECTED": "Rejected enrichment",
}

FIT_PRIORITY_OPTIONS = [
    "All",
    "High",
    "Medium",
    "Low",
    "Mismatch",
]

JOBS_PAGE_KEY = "jobs_current_page"
JOBS_FILTER_STATE_KEY = "jobs_filter_state"
JOBS_PAGE_SIZE_KEY = "jobs_page_size"
JOBS_PREVIOUS_PAGE_SIZE_KEY = "jobs_previous_page_size"


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


def default_date_range(
    job_dates: list[date],
    days: int = 7,
) -> tuple[date, date]:
    """Default the date filter to the newest useful window in the data."""
    if not job_dates:
        today = date.today()
        return today, today

    min_job_date = min(job_dates)
    max_job_date = max(job_dates)

    return (
        max(min_job_date, max_job_date - timedelta(days=days - 1)),
        max_job_date,
    )


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


def fit_priority_label(job: dict) -> str:
    """Return the shared review-priority label for filtering/display."""
    return str(
        job.get("fit_priority_label")
        or derive_fit_priority(job).label
    )


def fit_priority_source(job: dict) -> str:
    return str(
        job.get("fit_priority_source")
        or derive_fit_priority(job).source
    )


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
    pipeline_state = derive_job_pipeline_state(job)

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
                st.caption(pipeline_state.label)

            st.caption(
                APPLICATION_STATUS_LABELS.get(
                    application_status,
                    application_status,
                )
            )

            open_target = select_job_open_target(job)
            if open_target.url:
                st.link_button(open_target.label, open_target.url)

            if open_target.note:
                st.caption(open_target.note)

            if application_status == "new":
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

        title_fit = TITLE_FIT_LABELS.get(
            job.get("title_classification"),
            "Possible",
        )
        title_reason = job.get("title_filter_reason")
        title_score = job.get("title_match_score")
        title_caption = f"Title Fit: {title_fit}"

        if title_score is not None:
            title_caption += f" ({title_score})"

        if title_reason:
            title_caption += f" - {title_reason}"

        st.caption(title_caption)
        st.caption(
            "Fit priority: "
            f"{fit_priority_label(job)}"
        )
        st.caption(
            "Priority source: "
            f"{fit_priority_source(job)}"
        )
        st.caption(
            "Description: "
            f"{DESCRIPTION_STATE_LABELS.get(job.get('description_state'), 'Unknown')}"
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

        if job.get("enrichment_status"):
            status_text = job.get("enrichment_status")

            if status_text == "resolution_rejected":
                st.warning("Enrichment: identity mismatch rejected.")

        if (
            pipeline_state.details
            or job.get("enrichment_status")
            or job.get("official_job_url")
        ):
            with st.expander("Enrichment details"):
                st.caption(f"Pipeline state: {pipeline_state.label}")

                for detail_label, detail_value in pipeline_state.details:
                    st.caption(f"{detail_label}: {detail_value}")

                if job.get("identity_validation_reason"):
                    st.caption(
                        "Validation: "
                        f"{job['identity_validation_reason']}"
                    )

                if job.get("identity_confidence") is not None:
                    st.caption(
                        "Identity confidence: "
                        f"{job['identity_confidence']}"
                    )

                if job.get("resolved_candidate_title"):
                    st.caption(
                        "Candidate title: "
                        f"{job['resolved_candidate_title']}"
                    )

                if job.get("resolved_candidate_company"):
                    st.caption(
                        "Candidate company: "
                        f"{job['resolved_candidate_company']}"
                    )

                if job.get("resolved_candidate_url"):
                    st.caption(
                        "Resolved URL: "
                        f"{job['resolved_candidate_url']}"
                    )

                if job.get("official_url_status"):
                    st.caption(
                        "Official URL status: "
                        f"{job['official_url_status']}"
                    )

                if job.get("official_job_url"):
                    st.caption(
                        "Official URL: "
                        f"{job['official_job_url']}"
                    )

                if job.get("official_url_validation_reason"):
                    st.caption(
                        "Official validation: "
                        f"{job['official_url_validation_reason']}"
                    )

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
        value=DEFAULT_ENRICHMENT_LIMIT,
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
                resume_hash=st.session_state.get("resume_hash"),
            )
            st.write(f"Jobs selected: {enrichment_result.jobs_selected}")
            st.write(f"Jobs processed: {enrichment_result.jobs_processed}")
            st.write(
                "Eligible for enrichment: "
                f"{enrichment_result.eligible_for_enrichment}"
            )
            st.write(f"Never attempted: {enrichment_result.never_attempted}")
            st.write(
                "Needs official lookup: "
                f"{enrichment_result.needs_official_resolution}"
            )
            st.write(f"Descriptions updated: {enrichment_result.descriptions_updated}")
            st.write(f"Pages blocked: {enrichment_result.blocked}")
            st.write(f"No description found: {enrichment_result.no_description}")
            st.write(f"Fetch errors: {enrichment_result.fetch_error}")
            st.write(f"Not improved: {enrichment_result.not_improved}")
            st.write(
                f"Elapsed seconds: {enrichment_result.elapsed_seconds:.1f}"
            )
            if enrichment_result.stopped_for_time_budget:
                st.write("Stopped because the time budget was reached.")
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

    filter_cols = st.columns([1.4, 1.8, 1.8, 1.3, 1.3, 1])

    with filter_cols[0]:
        status_filter = st.selectbox(
            "Status",
            options=[
                "All",
                "New",
                "Applied",
                "Removed",
            ],
            index=1,
        )

    with filter_cols[1]:
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

    with filter_cols[2]:
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

    available_dates = sorted(set(job_dates))
    default_start_date, default_end_date = default_date_range(job_dates)

    start_date_input = default_start_date
    end_date_input = default_end_date

    start_date_key = "email_start_date_dropdown_v1"
    end_date_key = "email_end_date_dropdown_v1"

    for date_key in (start_date_key, end_date_key):
        if st.session_state.get(date_key) not in available_dates:
            st.session_state.pop(date_key, None)

    with filter_cols[3]:
        if available_dates:
            start_date_input = st.selectbox(
                "Start date",
                options=available_dates,
                index=available_dates.index(default_start_date),
                key=start_date_key,
                format_func=lambda value: value.strftime("%Y-%m-%d"),
            )
        else:
            st.caption("Load jobs first")

    with filter_cols[4]:
        if available_dates:
            end_date_input = st.selectbox(
                "End date",
                options=available_dates,
                index=available_dates.index(default_end_date),
                key=end_date_key,
                format_func=lambda value: value.strftime("%Y-%m-%d"),
            )

    if start_date_input <= end_date_input:
        selected_dates = (start_date_input, end_date_input)
    else:
        selected_dates = (end_date_input, start_date_input)

    with filter_cols[5]:
        scored_filter = st.selectbox(
            "AI score",
            options=[
                "All",
                "Scored",
                "Not scored",
            ],
        )

    extra_filter_cols = st.columns([2, 2, 2])

    with extra_filter_cols[0]:
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

    with extra_filter_cols[1]:
        fit_priority_filter = st.selectbox(
            "Fit / priority",
            options=FIT_PRIORITY_OPTIONS,
        )

    with extra_filter_cols[2]:
        description_state_filter = st.selectbox(
            "Description",
            options=[
                "All",
                *DESCRIPTION_STATE_LABELS.values(),
            ],
        )

    selected_title_fits = st.multiselect(
        "Title fit",
        options=[
            "Strong",
            "Possible",
            "Filtered out",
        ],
        default=[
            "Strong",
            "Possible",
            "Filtered out",
        ],
    )

    if isinstance(selected_dates, tuple):
        start_date, end_date = selected_dates
    else:
        start_date = selected_dates
        end_date = selected_dates

    filtered_jobs = all_jobs

    if status_filter != "All":
        selected_status = status_filter.casefold()
        filtered_jobs = [
            job
            for job in filtered_jobs
            if job.get("application_status", "new") == selected_status
        ]

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

    if fit_priority_filter != "All":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if fit_priority_label(job) == fit_priority_filter
        ]

    if description_state_filter != "All":
        filtered_jobs = [
            job
            for job in filtered_jobs
            if DESCRIPTION_STATE_LABELS.get(
                job.get("description_state")
            )
            == description_state_filter
        ]

    selected_title_categories = {
        category
        for category, label in TITLE_FIT_LABELS.items()
        if label in selected_title_fits
    }

    filtered_jobs = [
        job
        for job in filtered_jobs
        if job.get("title_classification", "POSSIBLE_MATCH")
        in selected_title_categories
    ]

    visible_applied_count = sum(
        1
        for job in all_jobs
        if job.get("application_status") == "applied"
    )
    visible_new_count = sum(
        1
        for job in all_jobs
        if job.get("application_status", "new") == "new"
    )
    visible_removed_count = sum(
        1
        for job in all_jobs
        if job.get("application_status") == "removed"
    )
    ai_scored_count = sum(
        1
        for job in all_jobs
        if job.get("ai_score") is not None
    )
    title_filtered_count = sum(
        1
        for job in all_jobs
        if job.get("title_classification") == "FILTERED_OUT"
    )
    needs_enrichment_count = sum(
        1
        for job in all_jobs
        if job.get("description_state") == "NEEDS_ENRICHMENT"
    )
    full_jd_count = sum(
        1
        for job in all_jobs
        if job.get("description_state") == "FULL_JD"
    )
    partial_jd_count = sum(
        1
        for job in all_jobs
        if job.get("description_state") == "PARTIAL_JD"
    )

    pagination_filter_state = (
        status_filter,
        source_filter,
        city_filter,
        start_date.isoformat() if start_date else None,
        end_date.isoformat() if end_date else None,
        scored_filter,
        recommendation_filter,
        fit_priority_filter,
        description_state_filter,
        tuple(selected_title_fits),
    )

    if st.session_state.get(JOBS_FILTER_STATE_KEY) != pagination_filter_state:
        st.session_state[JOBS_PAGE_KEY] = 1
        st.session_state[JOBS_FILTER_STATE_KEY] = pagination_filter_state

    pagination_cols = st.columns([1.1, 1, 1.1, 1.4, 4])

    with pagination_cols[3]:
        page_size = st.selectbox(
            "Jobs per page",
            options=list(PAGE_SIZE_OPTIONS),
            key=JOBS_PAGE_SIZE_KEY,
        )

    if st.session_state.get(JOBS_PREVIOUS_PAGE_SIZE_KEY) != page_size:
        st.session_state[JOBS_PAGE_KEY] = 1
        st.session_state[JOBS_PREVIOUS_PAGE_SIZE_KEY] = page_size

    preview_page = paginate_items(
        filtered_jobs,
        int(st.session_state.get(JOBS_PAGE_KEY, 1)),
        int(page_size),
    )
    st.session_state[JOBS_PAGE_KEY] = preview_page.current_page

    with pagination_cols[0]:
        previous_clicked = st.button(
            "Previous",
            disabled=(
                preview_page.total_pages == 0
                or preview_page.current_page <= 1
            ),
            key="jobs-page-previous",
        )

    with pagination_cols[2]:
        next_clicked = st.button(
            "Next",
            disabled=(
                preview_page.total_pages == 0
                or preview_page.current_page >= preview_page.total_pages
            ),
            key="jobs-page-next",
        )

    if previous_clicked:
        st.session_state[JOBS_PAGE_KEY] = preview_page.current_page - 1
    elif next_clicked:
        st.session_state[JOBS_PAGE_KEY] = preview_page.current_page + 1

    page = paginate_items(
        filtered_jobs,
        int(st.session_state.get(JOBS_PAGE_KEY, 1)),
        int(page_size),
    )
    st.session_state[JOBS_PAGE_KEY] = page.current_page

    page_label = (
        f"Page {page.current_page} of {page.total_pages}"
        if page.total_pages
        else "Page 0 of 0"
    )
    page_range_label = (
        f"Showing {page.start_number}-{page.end_number}"
        if page.total_items
        else "Showing 0"
    )

    with pagination_cols[1]:
        st.caption(page_label)

    st.caption(
        f"{page.total_items} jobs match filters. "
        f"{page_range_label}. {page_label}. "
        f"{len(all_jobs)} total reviewable jobs. "
        f"AI-scored: {ai_scored_count}. "
        f"Needs enrichment: {needs_enrichment_count}. "
        f"Full JD: {full_jd_count}. Partial JD: {partial_jd_count}. "
        f"New: {visible_new_count}. Applied: {visible_applied_count}. "
        f"Removed: {visible_removed_count}. "
        f"Title-classified as filtered out: {title_filtered_count}"
    )

    if not filtered_jobs:
        st.info("No jobs match the selected filters.")

    for job in page.items:
        render_job_listing(job)

with operations_tab:
    st.subheader("Pipeline Status")

    operation_metrics = load_pipeline_operation_metrics(
        resume_hash=st.session_state.get("resume_hash"),
        minimum_rule_score=int(daily_minimum_rule_score),
    )

    metric_cols = st.columns(4)
    metric_cols[0].metric("Raw jobs", operation_metrics["raw_jobs"])
    metric_cols[1].metric("Exact postings", operation_metrics["exact_postings"])
    metric_cols[2].metric("Reviewable jobs", operation_metrics["reviewable_jobs"])
    metric_cols[3].metric("AI-score eligible", operation_metrics["ai_score_eligible"])

    status_cols = st.columns(4)
    status_cols[0].metric("New", operation_metrics["new_jobs"])
    status_cols[1].metric("Applied", operation_metrics["applied_jobs"])
    status_cols[2].metric("Removed", operation_metrics["removed_jobs"])
    status_cols[3].metric("AI-scored", operation_metrics["ai_scored"])

    description_cols = st.columns(4)
    description_cols[0].metric("Needs enrichment", operation_metrics["needs_enrichment"])
    description_cols[1].metric("Full JD", operation_metrics["full_jd"])
    description_cols[2].metric("Partial JD", operation_metrics["partial_jd"])
    description_cols[3].metric("Rejected enrichment", operation_metrics["rejected_enrichment"])

    attempts = operation_metrics["enrichment_attempts"]
    attempt_cols = st.columns(5)
    attempt_cols[0].metric("Attempts", sum(attempts.values()))
    attempt_cols[1].metric("Successes", attempts.get("enriched", 0))
    attempt_cols[2].metric("Blocked", attempts.get("blocked", 0))
    attempt_cols[3].metric("No description", attempts.get("no_description", 0))
    attempt_cols[4].metric("Fetch errors", attempts.get("fetch_error", 0))

    st.caption(
        "Use the sidebar daily run controls to ingest email folders "
        "and score only jobs that do not already have a current AI score."
    )
