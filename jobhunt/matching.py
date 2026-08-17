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
    if path.suffix.lower() in (".docx", ".doc", ".pages", ".rtf"):
        # Reading these as text yields binary noise that would quietly poison
        # every score, so refuse rather than pretend it worked.
        raise RuntimeError(
            f"{path.suffix} isn't readable as text — export it to PDF first")
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
# Several tailored versions of the same resume. Each posting is scored against
# every variant and told which one to send. Three project layouts are
# recognised, in the order a document is tested for them:
#
#   1. \input{variant-swe}      one file per version, pulled into a shared
#                               main.tex — the Overleaf layout
#   2. %%% VARIANT: Name        markers inside a single file
#   3. \begin{variant}{Name}    environments inside a single file
#
# Whatever is shared (contact block, education, formatting) is prepended to
# each version, so a variant only carries what actually differs.
# --------------------------------------------------------------------------

# %%% VARIANT: Machine Learning   <- a LaTeX comment, so pdflatex ignores it
_VARIANT_MARKER = re.compile(r"^[ \t]*%+[ \t]*VARIANT[ \t]*:[ \t]*(.+?)[ \t]*$",
                             re.MULTILINE | re.IGNORECASE)
# \begin{variant}{Machine Learning} ... \end{variant}
_VARIANT_ENV = re.compile(
    r"\\begin\{variant\}\s*\{([^}]*)\}(.*?)\\end\{variant\}", re.DOTALL)
# \input{variant-swe} and %\input{variant-ml} alike — see split_input_variants.
_TEX_INPUT = re.compile(
    r"^[ \t]*%*[ \t]*\\(?:input|include)\{([^}]+)\}[ \t]*$", re.MULTILINE)

DEFAULT_VARIANT = "default"

# Words in a filename that describe the file rather than the version it holds.
_GENERIC_TOKENS = {
    "resume", "resumes", "cv", "variant", "variants", "version", "ver",
    "final", "draft", "copy", "latest", "new", "updated", "current",
    "main", "master", "doc", "document", "the",
}
_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+)*$|^(?:19|20)\d{2}$")
_SPLIT_TOKENS = re.compile(r"[\s._\-]+")


def _tokens(stem: str) -> list[str]:
    return [t for t in _SPLIT_TOKENS.split(str(stem)) if t]


def _pretty(token: str) -> str:
    """SWE stays SWE, swe becomes SWE, research becomes Research."""
    if token.isupper():
        return token
    return token.upper() if len(token) <= 3 else token[0].upper() + token[1:]


def variant_name(stem: str, drop: set[str] | frozenset[str] = frozenset()) -> str:
    """Turn a filename stem into a variant name: `variant-swe` -> `SWE`."""
    drop = {d.lower() for d in drop}
    toks = _tokens(Path(stem).stem)
    kept = [t for t in toks if t.lower() not in _GENERIC_TOKENS
            and t.lower() not in drop and not _VERSION_TOKEN.match(t.lower())]
    return " ".join(_pretty(t) for t in (kept or toks)) or DEFAULT_VARIANT


def variant_key(name: str) -> str:
    """Match key for a name. `SWE`, `swe`, and `Spicer-SWE-Resume` all agree."""
    toks = [t for t in _tokens(name)
            if t.lower() not in _GENERIC_TOKENS and not _VERSION_TOKEN.match(t.lower())]
    return "".join(re.sub(r"[^a-z0-9]", "", t.lower()) for t in (toks or _tokens(name)))


def name_batch(paths: list[str | Path],
               drop: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Name several files at once, dropping what every one of them shares.

    `Spicer-SWE-Resume.pdf`, `Spicer-ML-Resume.pdf`, `Spicer-Research-Resume.pdf`
    become `SWE`, `ML`, `Research` — "Spicer" is common to all three and so
    says nothing about which version you're looking at. `drop` adds tokens to
    discard regardless, which is how your own name goes when you add one file
    on its own and there's no batch to compare it against.
    """
    stems = [Path(p).stem for p in paths]
    sets = [{t.lower() for t in _tokens(s)} for s in stems]
    baseline = {d.lower() for d in drop}
    common = (set.intersection(*sets) if len(sets) > 1 else set()) | baseline

    def survives(stem: str, banned: set[str]) -> list[str]:
        return [t for t in _tokens(stem) if t.lower() not in banned
                and t.lower() not in _GENERIC_TOKENS
                and not _VERSION_TOKEN.match(t.lower())]

    # Back off if the shared part would swallow one of the names whole.
    if any(not survives(s, common) for s in stems):
        common = baseline
    return [variant_name(s, common) for s in stems]


def _resolve_input(base: Path, target: str) -> Path | None:
    for candidate in (target, f"{target}.tex"):
        p = base / candidate
        if p.is_file():
            return p
    return None


_RULE_LINE = re.compile(r"^[\s=\-*_~#]*$")


def _leading_note(src: str) -> str:
    """First real line of a file's opening comment block, used as a caption."""
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("%"):
            break
        text = stripped.lstrip("%").strip()
        if len(text) > 3 and not _RULE_LINE.match(text):
            return re.sub(r"\s+", " ", text)
    return ""


def split_input_variants(path: Path) -> list[dict]:
    """Variants from a one-file-per-version project.

    main.tex holds the preamble, contact block, and education, then \\inputs a
    single variant file at the bottom with the others commented out.
    Commented-out inputs are collected too: they're the versions you aren't
    compiling right now, not versions you deleted.
    """
    src = path.read_text(encoding="utf-8", errors="ignore")
    body_at = src.find(r"\begin{document}")

    hits = []
    for m in _TEX_INPUT.finditer(src):
        # Preamble helpers such as \input{glyphtounicode} are not variants.
        if body_at != -1 and m.start() < body_at:
            continue
        target = _resolve_input(path.parent, m.group(1).strip())
        if target and target != path:
            hits.append((m, target))
    if not hits:
        return []

    cut, shared = 0, []
    for m, _ in hits:
        shared.append(src[cut:m.start()])
        cut = m.end()
    shared.append(src[cut:])
    base = "".join(shared)

    names = name_batch([t for _, t in hits])
    out = []
    for (_, target), name in zip(hits, names):
        body = target.read_text(encoding="utf-8", errors="ignore")
        out.append({"name": name, "text": _strip_tex(base + "\n" + body),
                    "note": _leading_note(body), "source": target.name})
    return out


def split_variants(src: str) -> list[tuple[str, str]]:
    """Split a single-file resume source into [(variant_name, plain_text), ...].

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


def load_resume_documents(path: str | Path, name: str | None = None) -> list[dict]:
    """Every variant a single uploaded document yields.

    Returns [{"name", "text", "note", "source"}, ...]. A .tex may produce
    several; a .pdf or .txt is always exactly one, named after its filename.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"resume not found: {path}")

    if path.suffix.lower() == ".tex":
        inputs = split_input_variants(path)
        if inputs:
            return inputs
        src = path.read_text(encoding="utf-8", errors="ignore")
        found = split_variants(src)
        if len(found) > 1:
            return [{"name": n, "text": t, "note": "", "source": path.name}
                    for n, t in found]
        text, note = found[0][1], _leading_note(src)
    else:
        text, note = load_resume(path), ""

    return [{"name": name or variant_name(path.stem), "text": text,
             "note": note, "source": path.name}]


def load_resume_variants(path: str | Path) -> list[tuple[str, str]]:
    """Name/text pairs for one document — the shape the Scorer wants."""
    return [(d["name"], d["text"]) for d in load_resume_documents(path)]


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
