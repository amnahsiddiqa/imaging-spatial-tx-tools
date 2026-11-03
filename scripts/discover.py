"""Weekly discovery: search GitHub and Europe PMC for new imaging-based spatial
transcriptomics tools and add them to data/tools.yaml as `status: candidate`.

    python scripts/discover.py                       # last 21 days (the weekly default)
    python scripts/discover.py --since 2026-05-01 --min-stars 5

Also refreshes stars / last push / archived for every listed tool. Writes a run
summary (markdown) to --summary and `changes=true|false` to $GITHUB_OUTPUT.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re

import requests

from common import (INSTALL_RE, UA, Classifier, GitHub, load_config, load_tools, log,
                    repo_entry, repo_id, save_tools, slug_from_url, strip_private, today)

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TOOL_NAME_RE = re.compile(r"^([A-Za-z0-9][\w.+\-]{1,30})(?:\s*\([^)]{1,40}\))?\s*[:：]\s")


# ------------------------------------------------------------------------ GitHub

def score_repo(e: dict, clf: Classifier) -> int:
    meta, readme = e["_meta"], e["_readme"]
    strong = f"{meta['name']} {meta.get('description') or ''} {' '.join(meta.get('topics') or [])}"
    if clf.is_imaging(strong):
        score = 4
    else:  # platform named only in the README: more credit when it names several
        score = 2 if len(clf.detect_platforms(readme)) >= 2 else 1
    score += 2 if INSTALL_RE.search(readme) else 0
    score += 1 if meta.get("description") else -1
    score += 1 if meta.get("license") else 0
    score += 1 if meta.get("homepage") else 0
    score += 1 if len(readme) > 1500 else 0
    stars = meta.get("stargazers_count", 0)
    score += 2 if stars >= 25 else 1 if stars >= 5 else 0
    score += 2 if e.get("paper") else 0
    if clf.looks_like_analysis(strong):
        score -= 4
    elif clf.looks_like_analysis(readme[:2000], readme=True):
        score -= 2
    return score


def search_github(gh: GitHub, cfg: dict, clf: Classifier, min_stars: int,
                  known: set[str], excluded: set[str]) -> list[dict]:
    """Repos of any age that name an imaging platform and have at least min_stars.
    Brand-new 0-star repos are mostly one-off analyses; new tools with a preprint
    are picked up by the Europe PMC search instead."""
    gcfg = cfg["github"]
    window = f"fork:false stars:>={min_stars}"
    queries = [f"{q} in:name,description,readme {window}" for q in gcfg["queries"]]
    queries += [f"topic:{t} {window}" for t in gcfg["topic_queries"]]

    pool: dict[str, dict] = {}
    for q in queries:
        items = gh.search_repos(q)
        log(f"  {len(items):>4} repos  {q}")
        for it in items:
            rid = repo_id(it["full_name"])
            if rid in known or it["full_name"].lower() in excluded or it.get("archived"):
                continue
            pool.setdefault(rid, it)
    log(f"  {len(pool)} unseen repos to screen")

    out = []
    for it in pool.values():
        readme = gh.readme(it["full_name"])
        text = f"{it['name']} {it.get('description') or ''} {' '.join(it.get('topics') or [])} {readme}"
        if not clf.is_imaging(text) or clf.is_list(f"{text[:len(text) - len(readme)]} {readme[:600]}"):
            continue
        e = repo_entry(gh, it["full_name"], clf, meta=it, readme=readme)
        e["score"] = score_repo(e, clf)
        if e["score"] >= gcfg["min_score"]:
            e["source"] = "github-search"
            out.append(e)
    out.sort(key=lambda e: (-e["score"], -e["stars"]))
    return out


# ------------------------------------------------------------------- Europe PMC

def search_europepmc(cfg: dict, since: str) -> list[dict]:
    ec = cfg["europepmc"]
    platform = " OR ".join(f'ABSTRACT:"{t}"' for t in ec["abstract_terms"])
    method = " OR ".join([f'TITLE:"{t}"' for t in ec["method_title_terms"]]
                         + [f'ABSTRACT:"{t}"' for t in ec["method_abstract_phrases"]])
    query = (f'({platform}) AND (({method}) OR "github.com") '
             f"AND FIRST_PDATE:[{since} TO {today()}]")
    results, cursor = [], "*"
    s = requests.Session()
    s.headers["User-Agent"] = UA
    while True:
        r = s.get(EPMC, params={"query": query, "format": "json", "resultType": "core",
                                "pageSize": 100, "cursorMark": cursor}, timeout=60)
        r.raise_for_status()
        d = r.json()
        results += d["resultList"]["result"]
        if not d.get("nextCursorMark") or d["nextCursorMark"] == cursor or len(results) >= d["hitCount"]:
            break
        cursor = d["nextCursorMark"]
    log(f"  {len(results)} Europe PMC records since {since}")
    return results


def is_method_paper(rec: dict, cfg: dict) -> bool:
    title, abstract = rec.get("title", ""), rec.get("abstractText", "")
    if TOOL_NAME_RE.match(title) or "github.com" in abstract:
        return True
    terms = cfg["europepmc"]["method_title_terms"]
    return any(re.search(rf"\b{re.escape(t)}", title, re.I) for t in terms)


def find_repo_for_paper(gh: GitHub, name: str, doi: str, clf: Classifier) -> tuple[dict, str] | None:
    """Look for a repo named after the tool; accept it if its README cites the DOI
    or (failing that) if it is about imaging-based ST."""
    items = gh.search_repos(f"{name} in:name fork:false", max_pages=1)
    exact = [it for it in items if it["name"].lower().replace("-", "").replace("_", "")
             == name.lower().replace("-", "").replace("_", "")][:4]
    fallback = None
    for it in exact:
        readme = gh.readme(it["full_name"])
        if doi.lower() in readme.lower():
            return it, "doi-in-readme"
        if fallback is None and clf.is_imaging(f"{it.get('description') or ''} {readme}"):
            fallback = (it, "name-match")
    return fallback


# ------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="paper search window start, YYYY-MM-DD (default: today minus --days)")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--min-stars", type=int, help="GitHub star floor (default: config github.min_stars)")
    ap.add_argument("--no-github", action="store_true")
    ap.add_argument("--no-papers", action="store_true")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--summary", help="write a markdown run summary here")
    args = ap.parse_args()
    since = args.since or (dt.date.today() - dt.timedelta(days=args.days)).isoformat()

    cfg = load_config()
    clf, gh = Classifier(cfg), GitHub()
    tools = load_tools()
    by_id = {t["id"]: t for t in tools}
    known_dois = {t["paper"]["doi"].lower() for t in tools if t.get("paper")}
    known_dois |= {t["id"][4:] for t in tools if t["id"].startswith("doi:")}
    excluded = {s.lower() for s in cfg["exclude_repos"]}
    changes: list[str] = []

    # 1. Refresh metadata of tools already in the list.
    if not args.no_refresh:
        log("Refreshing listed tools")
        for t in tools:
            if t["status"] == "rejected" or not t["id"].startswith("github:"):
                continue
            meta = gh.repo(t["id"][7:])
            if not meta:
                if not t.get("note", "").startswith("repo not found"):
                    t["note"] = f"repo not found ({today()})"
                    changes.append(f"⚠️ `{t['name']}`: repo no longer found")
                continue
            if t.get("note", "").startswith("repo not found"):
                t.pop("note")
            if meta["html_url"] != t["url"]:
                changes.append(f"↪️ `{t['name']}` moved to {meta['html_url']}")
                t["url"] = meta["html_url"]
            if bool(meta.get("archived")) and not t.get("archived"):
                changes.append(f"🗄️ `{t['name']}` was archived by its owner")
            t.update(stars=meta.get("stargazers_count", 0), last_push=(meta.get("pushed_at") or "")[:10],
                     archived=bool(meta.get("archived")))

    new: list[dict] = []
    new_ids: set[str] = set()

    # 2. Papers (Europe PMC covers PubMed and bioRxiv/medRxiv preprints).
    if not args.no_papers:
        log("Searching Europe PMC")
        paper_new = []
        for rec in search_europepmc(cfg, since):
            doi = (rec.get("doi") or "").lower()
            if not doi or doi in known_dois or not is_method_paper(rec, cfg):
                continue
            title = re.sub(r"<[^>]+>", "", rec.get("title", "")).rstrip(".")
            abstract = re.sub(r"<[^>]+>", " ", rec.get("abstractText", ""))
            if not clf.is_imaging(f"{title} {abstract}"):
                continue
            paper = {"doi": doi, "title": title, "year": int(rec.get("pubYear") or 0) or None,
                     "preprint": rec.get("source") == "PPR"}
            m = TOOL_NAME_RE.match(title)
            name = m.group(1) if m else None
            slugs = [s for s in (slug_from_url(u) for u in re.findall(r"github\.com/[\w.\-/]+", abstract)) if s]
            hit, how = None, "abstract-link"
            if slugs:
                hit = gh.repo(slugs[0])
            elif name:
                found = find_repo_for_paper(gh, name, doi, clf)
                if found:
                    hit, how = found
            known_dois.add(doi)

            if hit and repo_id(hit["full_name"]) in by_id:
                t = by_id[repo_id(hit["full_name"])]
                if not t.get("paper") and how != "name-match":
                    t["paper"] = paper
                    if t["status"] != "rejected":
                        changes.append(f"📄 paper attached to `{t['name']}`: {title[:80]}")
                continue
            if hit and (repo_id(hit["full_name"]) in new_ids or hit["full_name"].lower() in excluded):
                continue
            if hit:
                e = repo_entry(gh, hit["full_name"], clf, meta=hit, want_paper=False)
                e["paper"] = paper
                e["category"] = clf.categorise(f"{e['name']} {e['description']}", f"{title} {abstract}",
                                               e["_readme"][:4000])
                e["platforms"] = sorted(set(e["platforms"]) | set(clf.detect_platforms(f"{title} {abstract}")))
                e["source"] = "europepmc"
                if how == "name-match":
                    e["note"] = "repo matched to paper by name only - check it is the right one"
            else:
                e = {"id": f"doi:{doi}", "name": name or title[:70], "url": f"https://doi.org/{doi}",
                     "category": clf.categorise(title, abstract), "description": title,
                     "platforms": clf.detect_platforms(f"{title} {abstract}"), "paper": paper,
                     "source": "europepmc", "note": "no code repository found yet"}
            paper_new.append(e)
            new_ids.add(e["id"])
        cap = cfg["europepmc"]["max_candidates_per_run"]
        if len(paper_new) > cap:
            changes.append(f"ℹ️ {len(paper_new) - cap} more paper hits beyond the cap of {cap} were skipped")
        new += paper_new[:cap]

    # 3. New GitHub repositories.
    if not args.no_github:
        min_stars = cfg["github"]["min_stars"] if args.min_stars is None else args.min_stars
        log(f"Searching GitHub (repos with >= {min_stars} stars not yet in the list)")
        found = [e for e in search_github(gh, cfg, clf, min_stars, set(by_id), excluded)
                 if e["id"] not in new_ids]
        cap = cfg["github"]["max_candidates_per_run"]
        if len(found) > cap:
            changes.append(f"ℹ️ {len(found) - cap} lower-scoring repo hits beyond the cap of {cap} were skipped")
        new += found[:cap]

    for e in new:
        e.update(status="candidate", added=today())
        tools.append(strip_private(e))
    save_tools(tools, cfg)

    n_repo = sum(e["id"].startswith("github:") for e in new)
    lines = [f"**Run {today()}** (window from {since}): {len(new)} new candidates "
             f"({n_repo} with code, {len(new) - n_repo} paper-only)."]
    lines += [f"- {c}" for c in changes]
    lines += [f"- 🆕 [{e['name']}]({e['url']}) → `{e['category']}`" for e in new]
    summary = "\n".join(lines) + "\n"
    log("\n" + summary)
    if args.summary:
        with open(args.summary, "w") as fh:
            fh.write(summary)
    meaningful = bool(new) or any(not c.startswith("ℹ️") for c in changes)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changes={'true' if meaningful else 'false'}\nnew={len(new)}\n")


if __name__ == "__main__":
    main()
