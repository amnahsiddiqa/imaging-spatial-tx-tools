# Maintaining the list

`data/tools.yaml` is the source of truth. `README.md` is generated from it, so never edit the README by hand. The [Render README](.github/workflows/render-readme.yml) Action re-renders it on every push that touches `data/`, `config/` or `scripts/`, on any branch, and fails if the YAML is invalid.

## Reviewing the weekly pull request

Every Monday [`weekly-discovery.yml`](.github/workflows/weekly-discovery.yml) runs `scripts/discover.py`. If it finds anything, it opens a PR from `auto/weekly-candidates` (or adds to it while it is still open). New entries have `status: candidate`. For each one, edit `data/tools.yaml` on that branch (the pencil icon in the PR's *Files changed* tab is enough):

| Decision | Edit |
|---|---|
| Keep it | `status: listed`, and fix `category`, `name`, `description`, `language` or `platforms` if needed |
| Not a fit | `status: rejected`, optionally with a `note:` saying why. It is stripped to a stub that keeps its paper's DOI, and neither the repo nor that paper is proposed again |
| Not sure yet | leave `status: candidate`. It stays under *Awaiting review* in the README |

Then merge. Check paper-only entries (`id: doi:...`) for a code link; if you find one, change `id` to `github:owner/repo` (lower case) and `url` to the repo.

## Adding a tool by hand

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=$(gh auth token)       # any token; raises the GitHub API rate limit
python scripts/add.py owner/repo:segmentation another/repo:qc
python scripts/build_readme.py
```

`add.py` fills the description, language, stars, detected platforms and the first DOI in the README that resolves in Crossref or DataCite. Check the paper it picked: a README sometimes cites a dataset or a dependency first.

## Fields

| Field | Meaning |
|---|---|
| `id` | `github:owner/repo` (lower case) or `doi:10.xxxx/...` for paper-only entries |
| `status` | `listed`, `candidate` or `rejected` |
| `category` | one of the ids in `config/search.yaml` (`uncategorised` is allowed for candidates only) |
| `platforms` | imaging technologies the tool names; detected automatically, edit freely |
| `paper` | `doi`, `title`, `year`, `preprint` (true for bioRxiv/medRxiv/arXiv and other preprint servers) |
| `stars`, `last_push`, `archived` | refreshed by the weekly run; do not edit |
| `score`, `source`, `added`, `note` | discovery bookkeeping |

## Tuning the search

Everything the search and the classifier use is in [`config/search.yaml`](config/search.yaml): platform patterns, GitHub queries, the star floor (`github.min_stars`), the score cut-off, per-run caps, exclusion patterns and category keywords. Try changes locally before pushing:

```bash
python scripts/discover.py --no-refresh --summary /tmp/summary.md   # writes candidates into data/tools.yaml
git diff --stat data/tools.yaml && git checkout data/tools.yaml      # inspect, then discard
```

To back-fill papers from an earlier window, run the workflow by hand (*Actions → Weekly discovery → Run workflow*) with a `since` date. Each run adds at most 40 papers and 40 repositories, best-scoring first; the rest come in later runs.
