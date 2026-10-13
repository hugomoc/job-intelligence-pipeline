"""Deterministic JD content evidence, independent of posting identity or transport."""
from __future__ import annotations

import re
from dataclasses import dataclass

MINIMUM_FULL_DESCRIPTION_WORDS = 80
SUBSTANTIAL_DESCRIPTION_WORDS = 200

# Explicit work/qualification sections, including headings embedded in extracted
# paragraphs. Generic words such as 'experience' are deliberately weaker.
STRONG_SECTION = re.compile(
    r"\b(?:responsibilit(?:y|ies)|requirements?|qualifications?|duties|must\s+have|"
    r"what\s+you(?:'ll|\s+will)\s+(?:do|work\s+on|bring)|"
    r"what\s+we(?:'re|\s+are)\s+looking\s+for)\b", re.I,
)
ROLE_SECTION = re.compile(
    r"(?:^|\n)\s*(?:who\s+you\s+are|about\s+you|(?:about\s+)?the\s+role|your\s+role)\s*[:.!]?\s*(?:\n|$)", re.I,
)
QUALIFICATION_SECTION = re.compile(
    r"(?:^|\n)\s*(?:skills|experience|preferred)\s*[:.!]?\s*(?:\n|$)", re.I,
)
APPLICANT_CONTENT = re.compile(
    r"\b(?:experience\s+(?:with|in|using)|(?:proficiency|proficient)\s+(?:in|with)|"
    r"you\s+(?:will|have|bring|are)|you'll|candidates?\s+(?:must|should)|"
    r"(?:required|preferred)\s+(?:skills|experience))\b", re.I,
)
WORK_CONTENT = re.compile(
    r"\b(?:build|design|develop|maintain|implement|deliver|manage|analyze|analyse|"
    r"collaborate|support|own|lead|model|evaluate|optimize|optimise)\b", re.I,
)
CHROME_LABEL = re.compile(
    r"\b(?:search results|filter by|browse jobs|view all jobs|job alerts|"
    r"sign in|sign up|privacy policy|cookie preferences|all rights reserved|navigation)\b", re.I,
)
SEARCH_PAGE = re.compile(r"\b(?:search results|search jobs|browse jobs|view all jobs|filter by)\b", re.I)


@dataclass(frozen=True)
class DescriptionQuality:
    full_jd: bool
    word_count: int
    strong_sections: tuple[str, ...]
    weak_signals: tuple[str, ...]
    reason: str


def assess_description_quality(description: object) -> DescriptionQuality:
    """Require minimum length and job-content signals; reject dominant chrome.

    One explicit JD section is sufficient. Otherwise substantial text must have
    at least two independent content signals, including applicant/role context.
    Repetition of a single weak keyword does not increase confidence.
    """
    text = str(description or '').replace('’', "'").replace('‘', "'")
    words = len(text.split())
    strong = tuple(sorted({m.group(0).casefold() for m in STRONG_SECTION.finditer(text)}))
    weak = []
    if ROLE_SECTION.search(text):
        weak.append('role_section')
    if QUALIFICATION_SECTION.search(text):
        weak.append('qualification_section')
    if APPLICANT_CONTENT.search(text):
        weak.append('applicant_content')
    if len({m.group(0).casefold() for m in WORK_CONTENT.finditer(text)}) >= 2:
        weak.append('work_content')
    chrome = list(CHROME_LABEL.finditer(text))
    # Distinct search/filter controls identify a listing page, not a single JD.
    listing = len({m.group(0).casefold() for m in SEARCH_PAGE.finditer(text)}) >= 2
    chrome_words = sum(len(m.group(0).split()) for m in chrome)
    dominant_chrome = len(chrome) >= 4 and chrome_words >= words * .25
    if words < MINIMUM_FULL_DESCRIPTION_WORDS:
        reason = 'below minimum description length'
    elif listing or dominant_chrome:
        reason = 'search/listing or navigation-dominated content'
    elif strong:
        return DescriptionQuality(True, words, strong, tuple(weak), 'explicit JD section evidence')
    elif words >= SUBSTANTIAL_DESCRIPTION_WORDS and len(weak) >= 2:
        return DescriptionQuality(True, words, strong, tuple(weak), 'multiple independent JD content signals')
    else:
        reason = 'insufficient JD structure/content evidence'
    return DescriptionQuality(False, words, strong, tuple(weak), reason)
