"""Shared deterministic JD requirement policy for scoring and eligibility.

No discovery, identity, database, or provider operations live here. Matcher
primitives are resolved inside helpers to keep the existing public API stable.
"""
from __future__ import annotations
import re
from typing import Any
from bs4 import BeautifulSoup
from src.ai.resume_profiler import ResumeProfile

# These families apply only when the fetched JD explicitly allows alternatives.
def _aliases():
    from src.ai.resume_matcher import TECH_ALIASES
    return {
    **TECH_ALIASES,
    "java": {"java"}, "python": {"python"},
    "clickhouse": {"clickhouse"},
    "postgres": {"postgres", "postgresql"},
    "sql_server": {"sql server", "microsoft sql server", "mssql"},
    "mysql": {"mysql"}, "debezium": {"debezium"},
    "kafka_connect": {"kafka connect"},
    "cdc": {"cdc", "change data capture"},
    "qlik_advana": {"qlik advana", "advana"},
    "salesforce": {"salesforce"},
    "fabric": {"microsoft fabric"}, "kubernetes": {"kubernetes", "k8s"},
    "tm1": {"tm1", "planning analytics", "turbointegrator"},
    "dbt": {"dbt"}, "ssis": {"ssis", "sql server integration services"},
    "service_bus": {"azure service bus"},
    "adf": {"azure data factory"}, "security_plus": {"security+", "security plus"},
    "netsuite": {"netsuite"}, "ramp": {"ramp"}, "pigment": {"pigment"},
}


def _without_scoped_platform_preference(clause: str) -> str:
    """Keep the main skill required when only its deployment variant is preferred.

    For example, "Databricks (on AWS preferred)" requires Databricks,
    while "(preferred)" after Databricks still makes that skill optional.
    """
    return re.sub(
        r"\(\s*(?:on|in|using|with)\b[^)]*?\b(?:preferred|ideally)\b[^)]*\)",
        "",
        clause,
        flags=re.I,
    )


def description_requirement_clauses(description: str) -> list[dict[str, Any]]:
    """Keep list/sentence boundaries and qualification-section provenance."""
    from src.ai.resume_matcher import normalize_match_text, regex_present, MANDATORY_REQUIREMENT_CONTEXT_PATTERNS
    soup = BeautifulSoup(description, "html.parser")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()
    for tag in soup.find_all(["li", "p", "h1", "h2", "h3", "h4", "br"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text().replace("\r", "\n")
    # Stored plain text may have lost HTML heading boundaries.
    text = re.sub(r"\b(Minimum qualifications|Required qualifications|Basic qualifications|Preferred qualifications|Desired Qualifications|Required Skills and Experience|Nice to have)\b", r"\n\1\n", text, flags=re.I)
    text = re.sub(r"\bU\.S\.(?:C\.)?", lambda match: match.group().replace(".", ""), text, flags=re.I)
    text = text.replace("’", "'")
    section = "unknown"
    clauses = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = normalize_match_text(line).strip(" .:")
        if len(line) < 90:
            if re.fullmatch(r"(?:minimum |required |basic )?(?:requirements|qualifications)|required(?: skills and experience)?|requirements for .+|what you (?:ll |will )?bring|what we (?:re |are )looking for|who you are|about you", heading):
                section = "required"
                continue
            if re.search(r"preferred qualifications|desired qualifications|nice.to.have|bonus qualifications", heading):
                section = "optional"
                continue
            if heading in {"responsibilities", "benefits", "about us", "about the company", "what you ll do", "what you will do", "why you ll love it here", "why you will love it here", "compensation and benefits", "compensation", "how we ll take care of you", "total rewards", "our tech stack"}:
                section = "other"
                continue
        # Do not split decimal years or e.g.; alternatives stay in one clause.
        for clause in re.split(r"(?<=[.!?])\s+(?=[A-Z])|;", line):
            optional = (section == "optional" or bool(re.search(
                r"\b(?:preferred|optional|nice.to.have|helpful|ideally|not required|not mandatory)\b",
                _without_scoped_platform_preference(clause),
                re.I,
            )))
            mandatory = not optional and (section == "required" or regex_present(clause, MANDATORY_REQUIREMENT_CONTEXT_PATTERNS) or bool(re.search(r"\b(?:required|requires|must|mandatory|minimum|need to have|expert skills|expert knowledge)\b|\b\d+\s*\+?\s*years?\b", clause, re.I)))
            # Essential must modify candidate experience/capability, not business
            # slogans ("our mission is essential") or boilerplate.
            if not optional and re.search(r"\bessential\b", clause, re.I) and re.search(r"\b(?:experience|knowledge|skills?|fluency|proficiency|certification)\b", clause, re.I):
                mandatory = True
            clauses.append({"clause": clause.strip(), "section": section, "mandatory": mandatory, "optional": optional})
    return clauses


# The AI hard-gap check requires a named AI capability and ownership terms
# in one mandatory JD clause. Generic references to agents/tools do not count.
AI_REQUIRED_TECH = re.compile(
    r"\b(?:rag|retrieval[\s-]+augmented[\s-]+generation|agentic"
    r"|llms?|large[\s-]+language[\s-]+models?|ai[\s-]+agents?"
    r"|ai[\s-]+tool[\s-]+integrations?|model[\s-]+context[\s-]+protocol)\b",
    re.I,
)
AI_DELIVERY = re.compile(
    r"\b(?:production|deploy\w*|build\w*|built|develop\w*|design\w*"
    r"|implement\w*|operat\w*|own\w*|integrat\w*|hands?[\s-]?on)\b",
    re.I,
)


# A required mention of LLMs, coding agents or AI-consumed metrics is NOT a
# requirement to own/deploy a production RAG or agent system. Alternatives that
# permit building a conventional semantic layer also cannot enforce RAG alone.
AI_OWNED_SYSTEM = re.compile(
    r"\b(?:rag|retrieval[\s-]+augmented[\s-]+generation|agentic"
    r"(?:\s+(?:ai|workflows?|systems?))?|ai[\s-]+agents?"
    r"|ai[\s-]+tool[\s-]+integrations?)\b", re.I
)
AI_OWNERSHIP = re.compile(
    r"\b(?:production|deploy\w*|operat\w*|own\w*|build\w*|built"
    r"|develop\w*|design\w*|implement\w*)\b", re.I
)
NON_AI_ALTERNATIVE = re.compile(
    r"\bor\b.{0,95}\b(?:semantic|metrics?|reporting|analytics)\b"
    r"|\b(?:semantic|metrics?|reporting|analytics)\b.{0,95}\bor\b", re.I
)


def explicit_production_ai_requirements(description: str) -> list[dict[str, Any]]:
    return [
        row for row in description_requirement_clauses(description)
        if row["mandatory"] and not row["optional"]
        and AI_OWNED_SYSTEM.search(row["clause"])
        and AI_OWNERSHIP.search(row["clause"])
        and not NON_AI_ALTERNATIVE.search(row["clause"])
        and not re.search(r"\b(?:comfortable using|familiar with|exposure to)\b", row["clause"], re.I)
    ]


def explicit_production_platform_clauses(
    description: str, platform: str,
) -> list[dict[str, Any]]:
    """Require an actual platform production skill, not lists or mere familiarity."""
    platform_pattern = r"\bdatabricks\b" if platform == "Databricks" else r"\b(?:machine[\s-]+learning|mlops|ml[\s-]+models?)\b"
    matching = []
    for row in description_requirement_clauses(description):
        clause = row["clause"]
        if not row["mandatory"] or row["optional"] or not re.search(platform_pattern, clause, re.I):
            continue
        if platform == "Databricks":
            if re.search(r"\b(?:understanding|familiarity|knowledge|exposure)\b", clause, re.I) and not re.search(
                r"\b(?:hands[\s-]?on|production|build\w*|develop\w*|operat\w*|deploy\w*|deep experience)\b", clause, re.I
            ):
                continue
            if re.search(r"\bor\b|\band/or\b|\bequivalent\b|\bcomparable\b", clause, re.I):
                continue
            # A group of example platforms is not a requirement to know every
            # vendor. E.g. BECU: (Fabric, Azure Databricks, BigQuery, Snowflake).
            if re.search(r"\b(?:platforms?|technologies?|components?|services?)\b.{0,45}\(", clause, re.I) and len(
                re.findall(r"\b(?:databricks|snowflake|big\s*query|bigquery|fabric|redshift)\b", clause, re.I)
            ) > 1:
                continue
            if re.search(r"\bincluding\b", clause, re.I) and re.search(
                r"\b(?:platforms?|technologies?|components?|services?)\b", clause, re.I
            ):
                continue
            if not re.search(
                r"\b(?:production|hands[\s-]?on|deep experience|expert\w*"
                r"|build\w*|built|develop\w*|design\w*|operat\w*|deploy\w*|maintain\w*|working)\b",
                clause, re.I,
            ):
                continue
        else:
            # Owning deployed models is distinct from ML concepts, data
            # preparation, support for data scientists or model familiarity.
            if not re.search(r"\b(?:deploy\w*|production|mlops)\b", clause, re.I):
                continue
            if re.search(r"\b(?:familiarity|basic|support|assist|collaborat\w*|facilitat\w*)\b", clause, re.I) and not re.search(
                r"\b(?:hands[\s-]?on|owned?|responsib\w*|deploy(?:ing|ed)?\s+(?:ml|machine[\s-]+learning|models?))\b",
                clause, re.I,
            ):
                continue
        matching.append(row)
    return matching


# Only explicit, single-platform professional-tenure minimums are enforceable.
# Generic years of data engineering and open platform alternatives are left to
# their own rules. A tool mentioned in a portfolio is not professional tenure.
TENURE_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("dbt Cloud", r"\bdbt[\s-]+cloud\b"),
    ("Azure Data Factory", r"\bazure\s+data\s+factory\b"),
    ("Microsoft Fabric", r"\bmicrosoft\s+fabric\b"),
    ("Databricks", r"\bdatabricks\b"),
    ("Snowflake", r"\bsnowflake\b"),
    ("dbt", r"\bdbt\b(?![\s-]+cloud\b)"),
    ("Apache Spark", r"\b(?:apache\s+spark|pyspark|spark)\b"),
    ("AWS Glue", r"\b(?:aws|amazon)\s+glue\b|\bglue\s+jobs?\b"),
    ("Apache Airflow", r"\b(?:apache\s+)?airflow\b"),
    ("Prefect", r"\bprefect\b"),
    ("Dagster", r"\bdagster\b"),
    ("Tableau", r"\btableau\b"),
    ("Power BI", r"\bpower\s+bi\b"),
    ("Domo", r"\bdomo\b"),
    ("Looker", r"\blooker\b(?!\s+studio)"),
)
TECHNOLOGY_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
# Keep each year's minimum tied to its own capability, not to an earlier
# unrelated minimum. For example, "3 years data engineering or analytics
# engineering, including 2 years building production dbt projects" requires
# TWO years of dbt, regardless of the unrelated "or" in the first phrase.
TENURE_ALTERNATIVE = re.compile(
    r"\b(?:or|equivalent|comparable|similar|such as|for example)\b|\be\.g\.", re.I
)
TENURE_CONTEXT = re.compile(
    r"\b(?:experience|hands?[\s-]?on|using|working|build\w*|develop\w*"
    r"|maintain\w*|operat\w*|production|implement\w*|in|with|on|of)\b",
    re.I,
)
GENERAL_EXPERIENCE_CONTEXT = re.compile(
    r"\b(?:data|analytics|software|database|platform)\s+engineering\b"
    r"|\bdata\s+(?:engineer|analyst|scientist)\b", re.I
)


def _platform_tenure_requirement(clause: str) -> tuple[str, str, int] | None:
    """Match a specific minimum to one platform within the same local phrase.

    Do not assign a generic preceding number to a later incidental tool
    mention, or treat an unrelated "X or Y" as a dbt alternative.
    """
    platforms = [
        (name, pattern)
        for name, pattern in TENURE_PLATFORMS
        if re.search(pattern, clause, re.I)
    ]
    # Multiple named platforms in one sentence may be alternatives or
    # distinct requirements; do not infer hard single-platform tenure.
    if len(platforms) != 1:
        return None
    name, pattern = platforms[0]
    hits = list(re.finditer(pattern, clause, re.I))
    if len(hits) != 1:
        return None
    hit = hits[0]

    candidates: list[tuple[int, int, int]] = []
    for years in TECHNOLOGY_YEARS.finditer(clause):
        if years.end() <= hit.start():
            between = clause[years.end():hit.start()]
            distance = len(between)
            if distance > 95:
                continue
            # "5 years data engineering, including dbt" is not 5 years dbt.
            if re.search(r"\b(?:including|includes|such as|for example)\b", between, re.I):
                continue
            if TENURE_ALTERNATIVE.search(between):
                continue
            if GENERAL_EXPERIENCE_CONTEXT.search(between):
                continue
            if between.strip() and not TENURE_CONTEXT.search(between):
                continue
            candidates.append((distance, years.start(), int(years.group(1))))
        elif hit.end() <= years.start():
            between = clause[hit.end():years.start()]
            distance = len(between)
            if distance > 70 or TENURE_ALTERNATIVE.search(between):
                continue
            if between.strip() and not TENURE_CONTEXT.search(between):
                continue
            candidates.append((distance, years.start(), int(years.group(1))))

    if not candidates:
        return None
    # The CLOSEST numbered minimum governs the named platform.
    _, pos, required_years = min(candidates)
    local_requirement = clause[pos:]
    if TENURE_ALTERNATIVE.search(local_requirement):
        return None
    return name, pattern, required_years


def required_platform_tenure_gaps(
    description: str,
    profile: ResumeProfile,
) -> tuple[list[str], list[str]]:
    """Find explicit mandatory professional platform-tenure gaps.

    A general job-tenure requirement is never evidence of tool-specific
    tenure. Portfolio projects and undated tool inventories do not prove
    professional experience. Credible but undated production evidence is a
    REVIEW uncertainty rather than an automatic SKIP.
    """
    missing: list[str] = []
    uncertain: list[str] = []
    project_only = re.compile(
        r"\b(?:personal|portfolio|coursework|self[\s-]?study|side[\s-]?project)\b",
        re.I,
    )
    professional = [
        str(item) for item in profile.production_skills
        if not project_only.search(str(item))
    ]
    # These are possible professional descriptions, but not dated proof.
    ambiguous_professional = [
        profile.professional_summary,
        *profile.data_engineering_capabilities,
        *profile.bi_analytics_skills,
    ]
    # Undated skill inventories cannot PROVE tenure, but an inventory mention
    # without a matching project-only claim warrants REVIEW, not a false SKIP.
    # A named portfolio technology (e.g. John Burns/dbt) is still project-only.
    undated_tools = [*profile.tools_platforms, *profile.databases_warehouses]
    project_text = " ".join(str(x) for x in profile.project_skills)

    for row in description_requirement_clauses(description):
        if not row["mandatory"] or row["optional"]:
            continue
        requirement = _platform_tenure_requirement(row["clause"])
        if requirement is None:
            continue
        name, pattern, required_years = requirement
        label = f"{required_years}+ years of professional {name} experience"

        exact_production = [
            item for item in professional if re.search(pattern, item, re.I)
        ]
        demonstrated_years = []
        for item in exact_production:
            forward = re.search(
                rf"\b(\d{{1,2}})\s*\+?\s*(?:years?|yrs?)\b.{{0,90}}{pattern}",
                item, re.I,
            )
            backward = re.search(
                rf"{pattern}.{{0,90}}\b(\d{{1,2}})\s*\+?\s*(?:years?|yrs?)\b",
                item, re.I,
            )
            for evidence in (forward, backward):
                if evidence:
                    demonstrated_years.append(int(evidence.group(1)))
        if demonstrated_years and max(demonstrated_years) >= required_years:
            continue

        inventory_hint = (
            any(re.search(pattern, str(tool), re.I) for tool in undated_tools)
            and not re.search(pattern, project_text, re.I)
        )
        if exact_production or inventory_hint or any(
            re.search(pattern, str(item), re.I)
            and not project_only.search(str(item))
            for item in ambiguous_professional
        ):
            uncertain.append(
                f"Required {label} is not verified by dated professional "
                "experience; project/undated tool use is insufficient."
            )
        else:
            missing.append(
                f"Major gap: {label} (not demonstrated in professional experience)"
            )

    return list(dict.fromkeys(missing)), list(dict.fromkeys(uncertain))

def clause_groups(text: str) -> set[str]:
    from src.ai.resume_matcher import contains_phrase
    groups = {name for name, aliases in _aliases().items()
              if any(contains_phrase(text, alias) for alias in aliases)}
    # Warehouse aliases do not authorize accepting a different cloud service.
    for cloud, aliases in {"aws": {"aws", "amazon web services"}, "gcp": {"gcp", "google cloud", "google cloud platform"}, "azure": {"azure", "microsoft azure"}}.items():
        if not any(contains_phrase(text, alias) for alias in aliases):
            groups.discard(cloud)
    if re.search(r"azure (?:data factory|service bus|integration services|event grid|event hubs|api management)", text, re.I) and not re.search(r"azure cloud|microsoft azure", text, re.I):
        groups.discard("azure")
    return groups


def validate_hard_gap_clause(gap: str, description: str, profile: ResumeProfile) -> dict[str, Any]:
    """Model labels are pointers, never mandatory-status evidence."""
    from src.ai.resume_matcher import significant_terms, CRITICAL_CAPABILITY_SIGNALS, regex_present, profile_to_text, requirement_is_demonstrated, requirement_requires_all, profile_fields_to_text, contains_phrase
    if AI_REQUIRED_TECH.search(gap):
        # Model-generated RAG/agentic gaps cannot establish mandatory status.
        # A concrete deployment requirement in the JD itself is required.
        clauses = explicit_production_ai_requirements(description)
        from src.ai.resume_matcher import production_resume_text
        signal = next(
            item for item in CRITICAL_CAPABILITY_SIGNALS
            if item.label.startswith("production RAG,")
        )
        demonstrated = regex_present(
            production_resume_text(profile), signal.production_patterns
        )
        checked = [
            dict(row, satisfied=demonstrated, allowed_groups=[],
                 resume_evidence=["production AI experience"] if demonstrated else [])
            for row in clauses
        ]
        return {
            "gap": gap,
            "keep_hard_gap": bool(clauses) and not demonstrated,
            "reason": (
                "mandatory production AI clause unsatisfied"
                if clauses and not demonstrated else
                "allowed clause satisfied" if clauses else
                "no explicit mandatory production AI requirement"
            ),
            "clauses": checked,
            "matched_clauses": clauses,
        }
    if "Databricks production" in gap:
        clauses = explicit_production_platform_clauses(description, "Databricks")
        from src.ai.resume_matcher import profile_fields_to_text
        professional = profile_fields_to_text(profile, (
            "production_skills", "professional_summary", "data_engineering_capabilities",
        ))
        demonstrated = bool(re.search(r"\bdatabricks\b", professional, re.I))
        checked = [dict(row, satisfied=demonstrated,
                        allowed_groups=[], resume_evidence=["professional Databricks"] if demonstrated else [])
                   for row in clauses]
        return {
            "gap": gap, "keep_hard_gap": bool(clauses) and not demonstrated,
            "reason": ("mandatory Databricks production clause unsatisfied"
                       if clauses and not demonstrated else
                       "allowed clause satisfied" if clauses else
                       "no explicit mandatory Databricks production requirement"),
            "clauses": checked, "matched_clauses": clauses,
        }
    if "production machine-learning model deployment" in gap:
        clauses = explicit_production_platform_clauses(description, "ML deployment")
        from src.ai.resume_matcher import production_resume_text
        professional = production_resume_text(profile)
        demonstrated = bool(re.search(
            r"\b(?:deployed?|production|mlops)\b.{0,75}"
            r"\b(?:machine[\s-]+learning|ml[\s-]+models?|model[\s-]+deployment)\b"
            r"|\b(?:machine[\s-]+learning|ml[\s-]+models?|model[\s-]+deployment)\b.{0,75}"
            r"\b(?:deployed?|production|mlops)\b", professional, re.I,
        ))
        checked = [dict(row, satisfied=demonstrated, allowed_groups=[],
                        resume_evidence=["production ML deployment"] if demonstrated else [])
                   for row in clauses]
        return {
            "gap": gap, "keep_hard_gap": bool(clauses) and not demonstrated,
            "reason": ("mandatory production ML deployment unsatisfied"
                       if clauses and not demonstrated else
                       "allowed clause satisfied" if clauses else
                       "no explicit mandatory production ML deployment requirement"),
            "clauses": checked, "matched_clauses": clauses,
        }
    groups = clause_groups(gap)
    terms = significant_terms(gap) - {"production", "expert", "required", "missing", "demonstrated", "major", "gap"}
    matches = []
    for row in description_requirement_clauses(description):
        cg = clause_groups(row["clause"])
        overlap = groups & cg
        tokens = terms & significant_terms(row["clause"])
        signal_match = any(
            signal.label in gap and regex_present(row["clause"], signal.job_patterns)
            for signal in CRITICAL_CAPABILITY_SIGNALS
        )
        if overlap or signal_match or (terms and len(tokens) >= max(1, (len(terms) + 1) // 2)):
            matches.append(row)
    personal = personal_requirement_evidence(gap, description, profile)
    if personal is not None:
        relevant = [row for row in description_requirement_clauses(description) if personal_fact_kind(row["clause"]) == personal["kind"]]
        mandatory_personal = [row for row in relevant if row["mandatory"]]
        keep = personal["state"] == "DOES_NOT_MEET" and bool(mandatory_personal)
        return {"gap": gap, "keep_hard_gap": keep, "reason": "mandatory personal condition explicitly unmet" if keep else "personal eligibility UNKNOWN; needs confirmation" if personal["state"] == "UNKNOWN" else "personal condition satisfied" if personal["state"] == "MEETS" else "mandatory status not established", "personal_evidence": personal, "clauses": [dict(row, satisfied=personal["state"] == "MEETS", allowed_groups=[], resume_evidence=[personal["evidence"]]) for row in mandatory_personal], "matched_clauses": relevant}
    mandatory = [row for row in matches if row["mandatory"]]
    evidence = profile_to_text(profile, include_certifications=False)
    decisions = []
    for row in mandatory:
        clause = row["clause"]
        cg = clause_groups(clause)
        alternatives = bool(re.search(r"\bor\b|such as|e\.g\.|examples? (?:include|including)|for example", clause, re.I))
        # A parenthesized list of modern data platforms gives acceptable
        # alternatives, not a mandate to know every listed provider.
        if len(cg) > 1 and re.search(
            r"\b(?:data|cloud|analytics)\s+platforms?\b.{0,50}\(", clause, re.I
        ):
            alternatives = True
        if re.search(r"(?:platforms?|tools?|systems?|warehouses?|databases?|environments?|services?)\s+like\b", clause, re.I):
            alternatives = True
        # A slash alone does not imply OR for unrelated compound capabilities.
        if "/" in clause and len(cg) > 1:
            named = [name for name in cg if name in {"java", "python", "clickhouse", "snowflake", "bigquery", "redshift", "postgres", "sql_server", "mysql", "debezium", "kafka_connect"}]
            alternatives = alternatives or len(named) > 1
        satisfied = requirement_is_demonstrated(clause, profile)
        allowed = set(cg)
        if alternatives and not requirement_requires_all(clause):
            if re.search(r"or (?:equivalent|comparable|similar)", clause, re.I):
                if "oltp" in clause.casefold() or "relational" in clause.casefold():
                    # Production systems prove the relational OLTP equivalent;
                    # portfolio mentions alone do not establish production tenure.
                    production = profile_fields_to_text(profile, ("production_skills", "databases_warehouses", "data_engineering_capabilities", "professional_summary"))
                    for name in {"postgres", "sql_server", "mysql"}:
                        if any(contains_phrase(production, alias) for alias in _aliases()[name]):
                            allowed.add(name)
                if cg & {"debezium", "kafka_connect", "cdc"}:
                    allowed |= {"cdc"}
            demonstrated = [name for name in allowed if any(contains_phrase(evidence, alias) for alias in _aliases()[name])]
            satisfied = bool(demonstrated) if cg else satisfied
        else:
            demonstrated = [name for name in cg if any(contains_phrase(evidence, alias) for alias in _aliases()[name])]
            if len(cg) > 1:
                satisfied = cg.issubset(set(demonstrated))
        decisions.append(dict(row, satisfied=satisfied, allowed_groups=sorted(allowed), resume_evidence=demonstrated))
    # A separately mandatory unsatisfied clause cannot be rescued by an optional mention.
    for row in decisions:
        if not row["satisfied"] and groups and not requirement_requires_all(row["clause"]) and re.search(r"such as|e\.g\.|examples? (?:include|including)", row["clause"], re.I):
            # Unmapped vendor examples cannot prove that each vendor is mandatory.
            # Broader domain/system-category fit remains a reviewable uncertainty.
            if re.search(r"source.of.truth systems|\bERP\b|\bWMS\b|\bTMS\b|\bCPM\b", row["clause"], re.I):
                row["vendor_examples_only"] = True
    unsatisfied = [row for row in decisions if not row["satisfied"] and not row.get("vendor_examples_only")]
    return {"gap": gap, "keep_hard_gap": bool(unsatisfied), "reason": "mandatory clause unsatisfied" if unsatisfied else "vendor examples not mandatory; broader capability needs review" if any(row.get("vendor_examples_only") for row in decisions) else "allowed clause satisfied" if decisions else "mandatory status not established", "clauses": decisions, "matched_clauses": matches}




def personal_fact_kind(text: str) -> str | None:
    value = text.casefold()
    patterns = {
        "citizenship": r"citizen|citizenship|permanent resident|itar|protected (?:person|individual)",
        "clearance": r"security clearance|secret.clearance|secret.level|public trust|federal clearance",
        "work_authorization": r"work authoriz|authoriz(?:ed|ation) to work|visa sponsorship|work visa|employment visa|sponsorship or transfer",
        "relocation": r"relocat(?:e|ion|ing)",
        "travel": r"travel (?:willingness|flexibility|up to|requirement)|willing to travel",
    }
    return next((kind for kind, pattern in patterns.items() if re.search(pattern, value)), None)


def personal_requirement_evidence(gap: str, description: str, profile: ResumeProfile, _kind: str | None = None) -> dict[str, Any] | None:
    kind = _kind or personal_fact_kind(gap)
    if kind is None:
        return None
    facts = profile.personal_eligibility
    state, evidence = "UNKNOWN", "No explicitly supplied structured fact"
    # Never infer personal status from the profile summary, residence or country
    # configured for actionability. Empty lists are explicit negatives; None is unknown.
    supporting = [row["clause"] for row in description_requirement_clauses(description) if personal_fact_kind(row["clause"]) == kind]
    text = (gap + " " + " ".join(supporting)).casefold()
    if kind == "work_authorization":
        countries = facts.authorized_work_countries
        if countries is not None and re.search(r"\b(?:u\.?s\.?|united states|america)\b", text):
            authorized = any(country.upper() in {"US", "USA", "UNITED STATES"} for country in countries)
            state = "MEETS" if authorized else "DOES_NOT_MEET"
            evidence = "Explicit authorized_work_countries"
        if "sponsor" in text:
            if facts.requires_sponsorship is True:
                state, evidence = "DOES_NOT_MEET", "Explicit requires_sponsorship=True"
            elif facts.requires_sponsorship is None and state == "MEETS":
                state, evidence = "UNKNOWN", "Sponsorship requirement not supplied"
    elif kind == "citizenship":
        countries = facts.citizenship_country_codes
        # ITAR admits several protected statuses; non-US citizenship is not proof
        # of failing those alternatives. No protected-status fact is inferred.
        if countries is not None and re.search(r"\b(?:u\.?s\.?|united states)\b", text):
            us = any(country.upper() in {"US", "USA", "UNITED STATES"} for country in countries)
            if us:
                state, evidence = "MEETS", "Explicit citizenship_country_codes includes US"
            elif not re.search(r"itar|resident|protected|\bor\b", text):
                state, evidence = "DOES_NOT_MEET", "Explicit citizenship_country_codes excludes US"
    elif kind == "clearance" and facts.security_clearances is not None:
        # Lacking a current clearance does not prove inability to obtain one.
        supplied = " ".join(facts.security_clearances).casefold()
        requested = "secret" if "secret" in text else "public trust" if "public trust" in text else None
        if requested and requested in supplied:
            state, evidence = "MEETS", "Explicit matching security_clearances"
        elif re.search(r"active|current|possess|hold", text):
            state, evidence = "DOES_NOT_MEET", "Explicit security_clearances lacks required current clearance"
    elif kind == "relocation" and facts.willing_to_relocate is not None:
        state, evidence = ("MEETS" if facts.willing_to_relocate else "DOES_NOT_MEET"), "Explicit willing_to_relocate"
    elif kind == "travel" and facts.willing_to_travel is not None:
        state, evidence = ("MEETS" if facts.willing_to_travel else "DOES_NOT_MEET"), "Explicit willing_to_travel"
        percent = re.search(r"(\d+)\s*%", gap)
        if state == "MEETS" and percent:
            if facts.max_travel_percent is None:
                state, evidence = "UNKNOWN", "Travel percentage not supplied"
            elif facts.max_travel_percent < int(percent.group(1)):
                state, evidence = "DOES_NOT_MEET", "Explicit maximum travel percentage below requirement"
    if _kind is None and kind == "citizenship" and re.search(r"security clearance|secret.level|public trust|federal clearance", gap, re.I):
        other = personal_requirement_evidence(gap, description, profile, _kind="clearance")
        if other["state"] == "DOES_NOT_MEET":
            state, evidence = "DOES_NOT_MEET", other["evidence"]
        elif other["state"] == "UNKNOWN" and state == "MEETS":
            state, evidence = "UNKNOWN", "Citizenship supplied; clearance condition remains unknown"
    return {"kind": kind, "state": state, "evidence": evidence}


# Eligibility can recognize a central delivery specialization even outside a
# formatted requirements section. Two independent delivery clauses are required
# unless one clause is explicitly mandatory or is supported by a title signal.
CENTRAL_SPECIALIZATIONS = (
    ("Kubernetes production systems", r"kubernetes|\bk8s\b", r"kubernetes|\bk8s\b"),
    ("Databricks production platform", r"databricks", r"databricks"),
    ("Microsoft Fabric platform", r"microsoft fabric", r"microsoft fabric"),
    ("IBM TM1 / Planning Analytics", r"\btm1\b|planning analytics|turbointegrator", r"\btm1\b|planning analytics|turbointegrator"),
    ("geospatial/raster engineering", r"geospatial|raster|remote sensing|coordinate reference", r"geospatial|raster|remote sensing|coordinate reference"),
    ("healthcare claims/EDI specialization", r"healthcare.*(?:data|experience)|medicare|\bcms\b|\bedi\b|\bx12\b|\bphi\b|\bhipaa\b", r"healthcare|medicare|\bcms\b|\bedi\b|\bx12\b|\bphi\b|\bhipaa\b"),
    ("low-latency market-data infrastructure", r"low.latency|market.data|feed.handlers|venue.connectivity|\bITCH\b|\bOUCH\b", r"low.latency|market.data|feed.handlers|venue.connectivity|\bITCH\b|\bOUCH\b"),
    ("Azure Data Factory integration", r"azure data factory|azure integration services", r"azure data factory|azure integration services"),
    ("marketing experimentation/incrementality", r"geo.holdout|incrementality|marketing.*(?:forecast|experiment)|experimentation.*marketing", r"geo.holdout|incrementality|marketing.*(?:forecast|experiment)|experimentation.*marketing"),
    ("enterprise production AI/LLM analytics", r"operationaliz.*(?:ai|llm)|deploy.*predictive models|production.*(?:llm|rag|agentic)", r"operationaliz.*(?:ai|llm)|deploy.*predictive models|production.*(?:llm|rag|agentic)"),
    ("Salesforce platform specialization", r"salesforce", r"salesforce"),
)


def central_specialization_gaps(description: str, title: str, profile: ResumeProfile) -> list[dict[str, Any]]:
    from src.ai.resume_matcher import profile_to_text, profile_fields_to_text
    if not isinstance(profile, ResumeProfile):
        return []
    evidence = profile_to_text(profile, include_certifications=False)
    production = profile_fields_to_text(profile, ("professional_summary", "production_skills", "data_engineering_capabilities", "bi_analytics_skills", "tools_platforms", "ai_ml_experience", "industries"))
    clauses = description_requirement_clauses(description)
    output = []
    for label, pattern, resume_pattern in CENTRAL_SPECIALIZATIONS:
        if label == "enterprise production AI/LLM analytics" and not explicit_production_ai_requirements(description):
            continue
        if re.search(resume_pattern, production if label == "enterprise production AI/LLM analytics" else evidence, re.I):
            continue
        matches = (
            explicit_production_ai_requirements(description)
            if label == "enterprise production AI/LLM analytics"
            else [row for row in clauses if not row["optional"] and re.search(pattern, row["clause"], re.I)]
        )
        unsatisfied = []
        for row in matches:
            # The same clause evaluator checks allowed alternatives; centrality
            # cannot rescue an incorrectly claimed missing Java/Python or vendor.
            check = validate_hard_gap_clause(label, "Requirements\n" + row["clause"], profile)
            if check["reason"] == "allowed clause satisfied":
                continue
            if label == "Databricks production platform" and not check["keep_hard_gap"]:
                continue
            if check["reason"].startswith("vendor examples"):
                continue
            if row["mandatory"] or re.search(r"build|design|develop|operat|deploy|own|lead|hands.on|primary focus|deep experience", row["clause"], re.I):
                unsatisfied.append(row)
        unique = {row["clause"] for row in unsatisfied}
        if any(row["mandatory"] for row in unsatisfied) or len(unique) >= 2 or (unique and re.search(pattern, title, re.I)):
            output.append({"gap": label, "clauses": unsatisfied, "profile_evidence": "No demonstrated matching central capability in supplied profile"})
    return output
