# gh-docs-index

Nightly builds a compact Lunr index of GitHub **issues** and **discussions** for use in the docs site.

## Run

- Manual test (limit to 200 items):

  - Actions → *Build GitHub Search Index* → *Run workflow* with e.g. `max = 100`, `full_rebuild = false`

- Outputs:
  - `out/github-docs.json` — metadata (id, type, number, title, url, labels, updated_at, excerpt)
  - `out/github-lunr-index.json` — Lunr index JSON

- Published (gh-pages):
  - `/latest/github-docs.json`
  - `/latest/github-lunr-index.json`

## Local dev

```bash
# With uv
uv run build-github-index --repo paperless-ngx/paperless-ngx --out out --max 300