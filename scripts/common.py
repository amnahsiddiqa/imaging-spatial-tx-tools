"""Shared helpers: config, tools.yaml I/O, GitHub/Crossref clients, classification."""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "search.yaml"
TOOLS_PATH = ROOT / "data" / "tools.yaml"
STATUSES = ("listed", "candidate", "rejected")
UA = "imaging-spatial-tx-tools/1.0 (+https://github.com/amnahsiddiqa/imaging-spatial-tx-tools)"

# Field order when writing tools.yaml, so diffs stay readable.
FIELD_ORDER = ["id", "name", "url", "status", "category", "description", "language",
               "platforms", "paper", "stars", "last_push", "archived", "source",
               "added", "score", "note"]

# DOI prefixes that are not papers (Zenodo, figshare, Dryad, OSF).
NON_PAPER_DOI_PREFIXES = ("10.5281/", "10.6084/", "10.5061/", "10.17605/")
# bioRxiv/medRxiv (old and new prefixes), arXiv, Research Square, SSRN, Preprints.org.
PREPRINT_DOI_PREFIXES = ("10.1101/", "10.64898/", "10.48550/", "10.21203/", "10.2139/", "10.20944/")


def today() -> str:
    return dt.date.today().isoformat()


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


def load_tools() -> list[dict]:
    if not TOOLS_PATH.exists():
        return []
    with open(TOOLS_PATH) as fh:
        return yaml.safe_load(fh) or []


def _ordered(entry: dict) -> dict:
    out = {k: entry[k] for k in FIELD_ORDER if entry.get(k) not in (None, "", [])}
    out.update({k: v for k, v in entry.items() if k not in out and v not in (None, "", [])})
    return out


def save_tools(tools: list[dict], cfg: dict) -> None:
    order = {c["id"]: i for i, c in enumerate(cfg["categories"])}
    status_rank = {s: i for i, s in enumerate(STATUSES)}
    cleaned = []
    for t in tools:
        if t["status"] == "rejected":
            # Rejected entries only need enough to stop them being proposed again.
            t = {k: t[k] for k in ("id", "name", "url", "status", "note") if t.get(k)}
        cleaned.append(_ordered(t))
    cleaned.sort(key=lambda t: (status_rank[t["status"]], order.get(t.get("category"), 99),
                                t["name"].lower()))
    TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TOOLS_PATH, "w") as fh:
        fh.write("# Source of truth for README.md. Review guide: CONTRIBUTING.md\n")
        yaml.safe_dump(cleaned, fh, sort_keys=False, allow_unicode=True, width=1000)


def repo_id(slug: str) -> str:
    return "github:" + slug.strip("/").lower()


def slug_from_url(url: str) -> str | None:
    m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)", url or "")
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2)).rstrip(".")
    if owner.lower() in {"orgs", "topics", "features", "sponsors", "settings"}:
        return None
    return f"{owner}/{repo}"


# --------------------------------------------------------------------------- HTTP

class GitHub:
    def __init__(self, token: str | None = None):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json", "User-Agent": UA,
                               "X-GitHub-Api-Version": "2022-11-28"})
        token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self._last_search = 0.0

    def _get(self, url: str, **kw) -> requests.Response:
        for attempt in range(5):
            r = self.s.get(url, timeout=30, **kw)
            if r.status_code in (403, 429) and ("rate limit" in r.text.lower() or r.status_code == 429):
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(5, min(reset - time.time() + 2, 900))
                log(f"  rate limited; sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    def repo(self, slug: str) -> dict | None:
        r = self._get(f"https://api.github.com/repos/{slug}")
        return r.json() if r.ok else None

    def readme(self, slug: str) -> str:
        r = self._get(f"https://api.github.com/repos/{slug}/readme",
                      headers={"Accept": "application/vnd.github.raw"})
        return r.text if r.ok else ""

    def search_repos(self, q: str, max_pages: int = 3) -> list[dict]:
        items = []
        for page in range(1, max_pages + 1):
            # Search API allows 30 requests/min with a token.
            time.sleep(max(0, 2.2 - (time.time() - self._last_search)))
            self._last_search = time.time()
            r = self._get("https://api.github.com/search/repositories",
                          params={"q": q, "per_page": 100, "page": page, "sort": "updated"})
            if not r.ok:
                log(f"  search failed ({r.status_code}): {q}")
                break
            batch = r.json().get("items", [])
            items += batch
            if len(batch) < 100:
                break
        return items


_http = requests.Session()
_http.headers["User-Agent"] = UA


def clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t or "")).strip().rstrip(".")


def _get_retry(url: str) -> requests.Response:
    """GET with backoff on rate limits, server errors and timeouts (Crossref throttles bursts)."""
    for attempt in range(5):
        try:
            r = _http.get(url, timeout=30)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
        except requests.RequestException:
            if attempt == 4:
                raise
        time.sleep(2 ** attempt)
    return r


def lookup_doi(doi: str) -> dict | None:
    """Verify a DOI against Crossref, then DataCite (arXiv). None if it does not resolve."""
    doi = doi.strip().rstrip(".").lower()
    if doi.startswith(NON_PAPER_DOI_PREFIXES):
        return None
    try:
        r = _get_retry(f"https://api.crossref.org/works/{doi}")
        if r.ok:
            m = r.json()["message"]
            if m.get("type") not in ("journal-article", "posted-content", "proceedings-article",
                                     "book-chapter"):
                return None
            year = (m.get("issued", {}).get("date-parts") or [[None]])[0][0]
            return {"doi": doi, "title": clean_title((m.get("title") or [""])[0]), "year": year,
                    "preprint": m.get("type") == "posted-content" or doi.startswith(PREPRINT_DOI_PREFIXES)}
        r = _get_retry(f"https://api.datacite.org/dois/{doi}")
        if r.ok:
            a = r.json()["data"]["attributes"]
            if (a.get("types") or {}).get("resourceTypeGeneral") not in ("Preprint", "Text", "JournalArticle"):
                return None
            return {"doi": doi, "title": clean_title((a.get("titles") or [{}])[0].get("title", "")),
                    "year": a.get("publicationYear"), "preprint": True}
    except (requests.RequestException, ValueError, KeyError) as e:
        log(f"  DOI lookup failed for {doi}: {e}")
    return None


DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>()\[\]{}|,;]+)", re.I)
ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)


def paper_from_text(text: str, max_tries: int = 4) -> dict | None:
    """First DOI (or arXiv ID) in a README that verifies as a paper or preprint."""
    found = []
    for m in DOI_RE.finditer(text):
        doi = re.sub(r"(\.full|\.abstract|\.pdf|v\d+)$", "", m.group(1).rstrip("._"), flags=re.I)
        if doi.lower() not in found:
            found.append(doi.lower())
    for m in ARXIV_RE.finditer(text):
        found.append(f"10.48550/arxiv.{m.group(1)}")
    for doi in found[:max_tries]:
        p = lookup_doi(doi)
        if p and p["title"]:
            return p
    return None


# ------------------------------------------------------------------ classification

def _rx(patterns):
    return [re.compile(p, re.I) for p in patterns]


class Classifier:
    def __init__(self, cfg: dict):
        self.platforms = {k: _rx(v) for k, v in cfg["platforms"].items()}
        self.generic = _rx(cfg["generic_terms"])
        self.context = _rx(cfg["context_terms"])
        self.categories = [(c["id"], _rx(c["keywords"])) for c in cfg["categories"]]
        self.analysis = _rx(cfg["analysis_repo_patterns"])
        self.analysis_readme = _rx(cfg["analysis_readme_patterns"])
        self.lists = _rx(cfg["list_patterns"])

    def detect_platforms(self, text: str) -> list[str]:
        return [name for name, pats in self.platforms.items() if any(p.search(text) for p in pats)]

    def is_imaging(self, text: str) -> bool:
        hit = self.detect_platforms(text) or any(p.search(text) for p in self.generic)
        return bool(hit) and any(p.search(text) for p in self.context)

    def is_list(self, text: str) -> bool:
        return any(p.search(text) for p in self.lists)

    def looks_like_analysis(self, text: str, readme: bool = False) -> bool:
        return any(p.search(text) for p in (self.analysis_readme if readme else self.analysis))

    def categorise(self, strong: str, medium: str = "", weak: str = "") -> str:
        best, best_score = "uncategorised", 0
        for cid, pats in self.categories:
            score = sum(3 * len(p.findall(strong)) + 2 * len(p.findall(medium)) for p in pats)
            score += min(sum(len(p.findall(weak)) for p in pats), 2)
            if score > best_score:
                best, best_score = cid, score
        return best if best_score >= 2 else "uncategorised"


INSTALL_RE = re.compile(r"pip install|conda install|install\.packages|BiocManager::install|"
                        r"devtools::install|remotes::install|Pkg\.add|cargo install|docker (pull|run)|"
                        r"nextflow run|setup\.py|pyproject", re.I)


def repo_entry(gh: GitHub, slug: str, clf: Classifier, meta: dict | None = None,
               readme: str | None = None, want_paper: bool = True) -> dict | None:
    """Build a tools.yaml entry from a GitHub repo (metadata, platforms, paper, category)."""
    meta = meta or gh.repo(slug)
    if not meta:
        return None
    slug = meta["full_name"]
    readme = gh.readme(slug) if readme is None else readme
    desc = (meta.get("description") or "").strip()
    topics = " ".join(meta.get("topics") or [])
    strong = f"{meta['name']} {desc} {topics}"
    paper = paper_from_text(readme) if want_paper else None
    return {
        "id": repo_id(slug),
        "name": meta["name"],
        "url": meta["html_url"],
        "category": clf.categorise(strong, paper["title"] if paper else "", readme[:4000]),
        "description": desc,
        "language": meta.get("language") or "",
        "platforms": clf.detect_platforms(f"{strong} {readme}"),
        "paper": paper,
        "stars": meta.get("stargazers_count", 0),
        "last_push": (meta.get("pushed_at") or "")[:10],
        "archived": bool(meta.get("archived")),
        "_readme": readme,
        "_meta": meta,
    }


def strip_private(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}
