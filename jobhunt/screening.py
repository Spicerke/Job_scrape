"""Hard requirement screening.

Scoring tells you how good a fit a job is. Screening answers a different
question: can you actually apply? A staff role requiring a TS/SCI clearance
and 8 years of experience might keyword-match beautifully and is still a
waste of your Friday.

Three of these checks read your resume directly:
  * degree level      - rejects PhD-only roles if you hold a bachelor's
  * graduation window - rejects roles targeting a class year that isn't yours
  * requirement gap   - rejects roles where your resume covers too few of the
                        technologies named in the Requirements section
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- years of exp
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)\b(?:[^.]{0,40}?(?:experience|exp\b))?",
    re.I,
)
WORD_YEARS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
WORD_YEARS_RE = re.compile(
    r"\b(" + "|".join(WORD_YEARS) + r")\s*(?:\+)?\s*(?:years?|yrs?)\b", re.I)


def min_years_required(text: str) -> int | None:
    found: list[int] = []
    for m in YEARS_RE.finditer(text):
        ctx = text[max(0, m.start() - 90): m.end() + 60].lower()
        if any(w in ctx for w in ("experience", "exp.", "background", "working")):
            found.append(int(m.group(1)))
    for m in WORD_YEARS_RE.finditer(text):
        ctx = text[max(0, m.start() - 90): m.end() + 60].lower()
        if "experience" in ctx:
            found.append(WORD_YEARS[m.group(1).lower()])
    return min(found) if found else None


# ------------------------------------------------------------------- degrees
DEGREE_RANK = {"bachelors": 1, "masters": 2, "phd": 3}
BACHELOR_RE = re.compile(r"\b(bachelor'?s?|b\.?s\.?|b\.?a\.?|undergraduate)\b", re.I)
MASTER_RE = re.compile(r"\b(master'?s?|m\.?s\.?|m\.?eng\.?)\b", re.I)
PHD_RE = re.compile(r"\b(ph\.?\s?d\.?|doctorate|doctoral)\b", re.I)
REQUIRED_NEAR = re.compile(r"\b(required|require|must have|minimum|necessary)\b", re.I)


def degree_floor(text: str) -> str | None:
    """Lowest degree the posting will accept, or None if not stated."""
    if BACHELOR_RE.search(text):
        return "bachelors"          # a BS is listed as acceptable somewhere
    has_ms, has_phd = MASTER_RE.search(text), PHD_RE.search(text)
    if not (has_ms or has_phd):
        return None
    hit = has_ms or has_phd
    window = text[max(0, hit.start() - 120): hit.end() + 120]
    if not REQUIRED_NEAR.search(window):
        return None                 # mentioned but not stated as a requirement
    return "masters" if has_ms else "phd"


GRAD_RE = re.compile(
    r"graduat\w*[^.]{0,60}?(20\d{2})|(20\d{2})[^.]{0,30}?graduat\w*", re.I)


def graduation_years(text: str) -> set[int]:
    years: set[int] = set()
    for m in GRAD_RE.finditer(text):
        for g in m.groups():
            if g:
                years.add(int(g))
    return years


# -------------------------------------------------------- requirement section
SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:basic |minimum |required |preferred )?"
    r"(requirements?|qualifications?|what you'?ll need|who you are|"
    r"what we'?re looking for|skills?(?: and experience)?)\s*:?\s*\n?",
    re.I,
)
NEXT_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:about (?:us|the team)|benefits?|compensation|salary|"
    r"perks|equal opportunity|why join|our values|how to apply|"
    r"what you'?ll do|responsibilit)",
    re.I,
)

# Technologies worth checking a resume against. Deliberately concrete: only
# things a resume would plausibly name, not soft skills.
TECH_VOCAB = {
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "golang",
    "rust", "ruby", "scala", "kotlin", "swift", "php", "perl", "matlab",
    "sql", "nosql", "postgres", "postgresql", "mysql", "mongodb", "redis",
    "cassandra", "elasticsearch", "snowflake", "bigquery", "spark", "hadoop",
    "kafka", "airflow", "dbt", "react", "angular", "vue", "svelte", "next.js",
    "node.js", "django", "flask", "fastapi", "rails", "spring", "graphql",
    "grpc", "docker", "kubernetes", "terraform", "ansible", "jenkins",
    "aws", "gcp", "azure", "linux", "git", "ci/cd", "pytorch", "tensorflow",
    "keras", "scikit-learn", "numpy", "pandas", "matplotlib", "jax",
    "hugging face", "transformers", "opencv", "cuda", "machine learning",
    "deep learning", "computer vision", "nlp", "natural language processing",
    "reinforcement learning", "graph neural network", "llm",
    "distributed systems", "microservices", "data pipeline", "etl",
    "unit testing", "html", "css", "figma", "tableau",
}


def requirements_section(text: str) -> str:
    m = SECTION_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = NEXT_SECTION_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest[:3000]


def _has_term(term: str, text: str) -> bool:
    return re.search(
        r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text
    ) is not None


class Screener:
    def __init__(self, config: dict, resume_text: str = ""):
        s = config.get("screening", {}) or {}
        p = config.get("profile", {}) or {}
        self.max_years = s.get("max_years_experience")
        self.reject_title_terms = [t.lower() for t in (s.get("reject_title_terms") or [])]
        self.reject_body_terms = [t.lower() for t in (s.get("reject_body_terms") or [])]
        self.require_any = [t.lower() for t in (s.get("require_any_terms") or [])]
        self.locations = [l.lower() for l in (s.get("locations") or [])]
        self.allow_remote = bool(s.get("allow_remote", True))
        self.min_description_chars = int(s.get("min_description_chars", 200))

        # resume-driven checks
        self.resume = (resume_text or "").lower()
        self.degree_level = str(p.get("degree_level", "bachelors")).lower()
        self.enforce_degree = bool(s.get("enforce_degree_level", True))
        self.enforce_grad = bool(s.get("enforce_graduation_year", False))
        self.grad_year = None
        grad = str(p.get("graduation") or "")
        if re.match(r"^\d{4}", grad):
            self.grad_year = int(grad[:4])
        self.min_coverage = float(s.get("min_requirement_coverage", 0.0) or 0.0)

        vocab = set(TECH_VOCAB)
        vocab |= {k.lower() for k in (config.get("keywords", {}).get("skills") or {})}
        self.vocab = vocab

    # ------------------------------------------------------------------
    def requirement_gap(self, description: str) -> tuple[float, list[str], list[str]]:
        """Fraction of the technologies named in the requirements section that
        also appear on your resume, plus the have/missing lists."""
        section = requirements_section(description).lower()
        if not section:
            return 1.0, [], []
        named = sorted(t for t in self.vocab if _has_term(t, section))
        if len(named) < 4:
            return 1.0, named, []      # too little signal to judge fairly
        have = [t for t in named if _has_term(t, self.resume)]
        missing = [t for t in named if t not in have]
        return len(have) / len(named), have, missing

    def screen(self, title: str, description: str, location: str | None,
               remote: bool) -> tuple[str, str]:
        """Return ('pass'|'reject', reason)."""
        title_l = (title or "").lower()
        body = (description or "").lower()
        loc_l = (location or "").lower()

        for term in self.reject_title_terms:
            if _has_term(term, title_l):
                return "reject", f"title contains '{term}'"

        if re.search(r"\b(II|III|IV|V)\b", title or ""):
            return "reject", "title indicates a leveled senior role"

        if self.max_years is not None and body:
            need = min_years_required(body)
            if need is not None and need > self.max_years:
                return "reject", f"requires {need}+ yrs (limit {self.max_years})"

        for term in self.reject_body_terms:
            if _has_term(term, body):
                return "reject", f"description mentions '{term}'"

        if self.enforce_degree and body:
            floor = degree_floor(description or "")
            if floor and DEGREE_RANK[floor] > DEGREE_RANK.get(self.degree_level, 1):
                label = {"masters": "master's", "phd": "PhD"}[floor]
                return "reject", f"requires a {label}"

        if self.enforce_grad and self.grad_year and body:
            years = graduation_years(description or "")
            if years and self.grad_year not in years:
                listed = ", ".join(str(y) for y in sorted(years))
                return "reject", f"targets {listed} grads (you're {self.grad_year})"

        if self.require_any:
            haystack = f"{title_l} {body}"
            if not any(_has_term(t, haystack) for t in self.require_any):
                return "reject", "missing all required terms"

        if self.locations:
            loc_ok = any(_has_term(l, loc_l) or l in loc_l for l in self.locations)
            if not loc_ok and not (remote and self.allow_remote):
                return "reject", f"location '{location or 'unknown'}' out of range"

        if description is not None and len(description) < self.min_description_chars:
            return "reject", "description too short to evaluate"

        if self.min_coverage > 0 and self.resume and body:
            coverage, _have, missing = self.requirement_gap(description or "")
            if coverage < self.min_coverage:
                top = ", ".join(missing[:4])
                return "reject", f"resume covers {coverage:.0%} of requirements (missing {top})"

        return "pass", ""
