"""Resume parsing plus job scoring.

Score is a 0-100 blend of three signals:
  * keyword_score   - weighted config keywords found in the posting
  * resume_similarity - TF-IDF cosine between your resume and the posting
  * title_score     - how well the job title matches your target roles

Zero heavyweight dependencies: TF-IDF is implemented directly so this can
run from cron on a laptop or a $5 VPS without numpy or scikit-learn.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "its", "of", "on", "or", "our", "that", "the", "this", "to",
    "we", "will", "with", "you", "your", "their", "they", "can", "all", "who",
    "work", "team", "role", "job", "company", "position", "candidate", "us",
    "including", "across", "within", "other", "more", "such", "also", "which",
    "not", "but", "any", "may", "his", "her", "into", "than", "them", "these",
}


def tokenize(text: str) -> list[str]:
    out = []
    for tok in TOKEN_RE.findall(text.lower()):
        tok = tok.strip(".-")
        if len(tok) < 2 or tok in STOPWORDS or tok.isdigit():
            continue
        out.append(tok)
    return out


# --------------------------------------------------------------------------
# resume loading
# --------------------------------------------------------------------------

_TEX_COMMENT = re.compile(r"(?<!\\)%.*")
_TEX_CMD = re.compile(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?")
_TEX_BRACES = re.compile(r"[{}$&~^_\\]")


def _strip_tex(src: str) -> str:
    body = src.split(r"\begin{document}")[-1]
    body = _TEX_COMMENT.sub("", body)
    body = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", body)
    body = _TEX_CMD.sub(" ", body)
    body = _TEX_BRACES.sub(" ", body)
    return re.sub(r"\s+", " ", body).strip()


def load_resume(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"resume not found: {path}")
    if path.suffix.lower() == ".tex":
        return _strip_tex(path.read_text(encoding="utf-8", errors="ignore"))
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader  # optional dependency
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PDF resume needs `pip install pypdf`, or point resume_path "
                "at a .tex/.txt file instead"
            ) from exc
        return " ".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------
# resume variants
#
# One .tex file, several tailored versions of the same resume. Each posting is
# scored against every variant and told which one to send. Anything before the
# first marker (contact block, skills, education) is shared by all of them, so
# a variant only contains what actually differs.
# --------------------------------------------------------------------------

# %%% VARIANT: Machine Learning   <- a LaTeX comment, so pdflatex ignores it
_VARIANT_MARKER = re.compile(r"^[ \t]*%+[ \t]*VARIANT[ \t]*:[ \t]*(.+?)[ \t]*$",
                             re.MULTILINE | re.IGNORECASE)
# \begin{variant}{Machine Learning} ... \end{variant}
_VARIANT_ENV = re.compile(
    r"\\begin\{variant\}\s*\{([^}]*)\}(.*?)\\end\{variant\}", re.DOTALL)

DEFAULT_VARIANT = "default"


def split_variants(src: str) -> list[tuple[str, str]]:
    """Split a resume source into [(variant_name, plain_text), ...].

    Recognises `%%% VARIANT: Name` comment markers and `\\begin{variant}{Name}`
    environments. A document using neither comes back as a single variant, so
    a plain one-version resume keeps working untouched.
    """
    env_matches = list(_VARIANT_ENV.finditer(src))
    if env_matches:
        shared = src[: env_matches[0].start()] + _VARIANT_ENV.sub("", src[env_matches[-1].end():])
        return [(m.group(1).strip() or DEFAULT_VARIANT, _strip_tex(shared + "\n" + m.group(2)))
                for m in env_matches]

    marks = list(_VARIANT_MARKER.finditer(src))
    if marks:
        shared = src[: marks[0].start()]
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(src)
            body = src[m.end():end]
            out.append((m.group(1).strip() or f"variant {i + 1}",
                        _strip_tex(shared + "\n" + body)))
        return out

    return [(DEFAULT_VARIANT, _strip_tex(src))]


def load_resume_variants(path: str | Path) -> list[tuple[str, str]]:
    """Variant split for .tex sources; everything else is a single variant."""
    path = Path(path)
    if path.suffix.lower() == ".tex":
        if not path.exists():
            raise FileNotFoundError(f"resume not found: {path}")
        return split_variants(path.read_text(encoding="utf-8", errors="ignore"))
    return [(DEFAULT_VARIANT, load_resume(path))]


# --------------------------------------------------------------------------
# TF-IDF
# --------------------------------------------------------------------------

class TfidfIndex:
    """Builds IDF weights from a corpus of job postings."""

    def __init__(self, documents: list[str]):
        self.n_docs = max(len(documents), 1)
        df: Counter[str] = Counter()
        for doc in documents:
            df.update(set(tokenize(doc)))
        self.df = df

    def idf(self, term: str) -> float:
        return math.log((self.n_docs + 1) / (self.df.get(term, 0) + 1)) + 1.0

    def vector(self, text: str) -> dict[str, float]:
        counts = Counter(tokenize(text))
        if not counts:
            return {}
        vec = {t: (1 + math.log(c)) * self.idf(t) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    @staticmethod
    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(w * b.get(t, 0.0) for t, w in a.items())


# --------------------------------------------------------------------------
# keyword + title matching
# --------------------------------------------------------------------------

def _phrase_present(phrase: str, haystack: str) -> bool:
    """Word-boundary-safe search that tolerates c++, node.js, etc."""
    pattern = r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


class Scorer:
    def __init__(self, config: dict, resume_text: str, index: TfidfIndex,
                 variants: list[tuple[str, str]] | None = None):
        self.cfg = config
        self.index = index
        self.resume_vec = index.vector(resume_text)
        self.resume_lower = resume_text.lower()
        # Every variant shares the keyword and title signals — only the TF-IDF
        # arm moves — so scoring all of them costs one extra cosine each.
        self.variants = variants or [(DEFAULT_VARIANT, resume_text)]
        self.variant_vecs = [(n, index.vector(t)) for n, t in self.variants]

        kw = config.get("keywords", {}) or {}
        self.skills: dict[str, float] = {
            k.lower(): float(v) for k, v in (kw.get("skills") or {}).items()
        }
        self.bonus: dict[str, float] = {
            k.lower(): float(v) for k, v in (kw.get("bonus") or {}).items()
        }
        # Normalizing against *every* skill would mean a perfect job still
        # scores ~0.4, since no posting lists your whole resume. Instead the
        # ceiling is the top-K heaviest skills: match those and you're at 1.0.
        top_k = int((config.get("search") or {}).get("keyword_saturation", 8))
        top_weights = sorted(self.skills.values(), reverse=True)[:top_k]
        self.max_kw_weight = sum(top_weights) or 1.0

        search = config.get("search", {}) or {}
        self.role_patterns = [p.lower() for p in (search.get("role_patterns") or [])]
        self.weights = {
            "keyword": 0.40, "resume": 0.35, "title": 0.25,
            **(search.get("weights") or {}),
        }

    def title_score(self, title: str) -> float:
        t = title.lower()
        best = 0.0
        for pattern in self.role_patterns:
            words = pattern.split()
            hits = sum(1 for w in words if _phrase_present(w, t))
            if hits == len(words):
                return 1.0
            best = max(best, hits / max(len(words), 1) * 0.7)
        return best

    def score(self, title: str, description: str, location: str = "") -> dict:
        blob = f"{title}\n{description}\n{location}".lower()

        matched, missing, earned = [], [], 0.0
        for term, weight in self.skills.items():
            if _phrase_present(term, blob):
                matched.append(term)
                earned += weight
            else:
                missing.append(term)
        for term, weight in self.bonus.items():
            if _phrase_present(term, blob):
                matched.append(term)
                earned += weight

        keyword_score = min(earned / self.max_kw_weight, 1.0)
        t_score = self.title_score(title)
        job_vec = self.index.vector(blob)

        def total_for(similarity: float) -> float:
            # cosine on long JD text realistically tops out around 0.45; rescale
            return (
                self.weights["keyword"] * keyword_score
                + self.weights["resume"] * min(similarity / 0.45, 1.0)
                + self.weights["title"] * t_score
            ) * 100

        sims = {n: TfidfIndex.cosine(v, job_vec) for n, v in self.variant_vecs}
        variant_scores = {n: round(total_for(s), 1) for n, s in sims.items()}
        best_variant = max(sims, key=lambda n: sims[n])
        similarity = sims[best_variant]

        matched.sort(key=lambda m: -self.skills.get(m, self.bonus.get(m, 1)))
        return {
            "score": round(total_for(similarity), 1),
            "keyword_score": round(keyword_score, 3),
            "resume_similarity": round(similarity, 3),
            "title_score": round(t_score, 3),
            "matched": matched,
            "missing": sorted(missing, key=lambda m: -self.skills[m])[:8],
            "best_variant": best_variant,
            "variant_scores": variant_scores,
        }
