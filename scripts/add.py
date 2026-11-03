"""Add tools to data/tools.yaml by GitHub slug, filling metadata from the API.

    python scripts/add.py kharchenkolab/Baysor:segmentation dcjones/proseg
    python scripts/add.py --file slugs.txt          # one "owner/repo[:category]" per line

Without a category the keyword classifier picks one. Then run build_readme.py.
"""
from __future__ import annotations

import argparse

from common import (Classifier, GitHub, load_config, load_tools, log, repo_entry,
                    repo_id, save_tools, strip_private, today)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*", help="owner/repo or owner/repo:category")
    ap.add_argument("--file", help="text file with one slug[:category] per line (# comments ok)")
    ap.add_argument("--status", default="listed", choices=["listed", "candidate"])
    ap.add_argument("--no-paper", action="store_true", help="skip README DOI lookup")
    args = ap.parse_args()

    specs = list(args.slugs)
    if args.file:
        with open(args.file) as fh:
            specs += [ln.split("#")[0].strip() for ln in fh if ln.split("#")[0].strip()]

    cfg = load_config()
    valid = {c["id"] for c in cfg["categories"]}
    clf, gh = Classifier(cfg), GitHub()
    tools = load_tools()
    known = {t["id"] for t in tools}

    added, failed = 0, []
    for spec in specs:
        slug, _, cat = spec.partition(":")
        if cat and cat not in valid:
            raise SystemExit(f"Unknown category '{cat}' for {slug}. Valid: {', '.join(sorted(valid))}")
        if repo_id(slug) in known:
            log(f"skip (already in list): {slug}")
            continue
        e = repo_entry(gh, slug, clf, want_paper=not args.no_paper)
        if not e:
            failed.append(slug)
            log(f"NOT FOUND: {slug}")
            continue
        if e["id"] in known:  # renamed repo resolving to one we already have
            log(f"skip (redirects to listed {e['id']}): {slug}")
            continue
        e.update(status=args.status, source="manual", added=today())
        if cat:
            e["category"] = cat
        tools.append(strip_private(e))
        known.add(e["id"])
        added += 1
        p = e["paper"]
        log(f"added {e['name']:<28} {e['category']:<13} {','.join(e['platforms']) or '-':<30} "
            f"{(p['doi'] + ' | ' + p['title'][:60]) if p else ''}")

    save_tools(tools, cfg)
    log(f"\n{added} added, {len(failed)} not found: {' '.join(failed)}")


if __name__ == "__main__":
    main()
