#!/usr/bin/env python3
import argparse, asyncio, json, os, re, sys, pathlib, time
from datetime import datetime, timezone
from dateutil.parser import isoparse

import httpx
from lunr import lunr

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"

def excerpt(text: str, n: int = 400) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:n]

def load_state(cache_dir: str):
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, "state.json")
    if os.path.exists(p):
        return p, json.load(open(p))
    return p, {"last_run": None, "since": None}

def save_state(p: str, state: dict):
    with open(p, "w") as f:
        json.dump(state, f, indent=2)

def auth_headers(token: str | None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "gh-docs-index"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

async def _request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    # Simple retry/backoff for secondary rate-limits or transient 5xx
    retries = 5
    backoff = 1.0
    for i in range(retries):
        resp = await client.request(method, url, **kwargs)
        if resp.status_code in (429, 502, 503, 504):
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()

async def crawl_issues(repo: str, token: str | None, since_iso: str | None, max_items: int | None):
    """
    REST /issues (updated desc). Pagination: per_page=100.
    """
    out = []
    params = {"state": "all", "per_page": 100, "sort": "updated", "direction": "desc"}
    if since_iso:
        params["since"] = since_iso

    async with httpx.AsyncClient(headers=auth_headers(token), timeout=30.0) as client:
        page = 1
        while True:
            q = params | {"page": page}
            data = await _request_json(client, "GET", f"{API}/repos/{repo}/issues", params=q)
            if not data:
                break
            for it in data:
                is_pr = "pull_request" in it
                if is_pr:
                    continue
                out.append({
                    "id": f"I{it['id']}" if not is_pr else f"P{it['id']}",
                    "type": "pr" if is_pr else "issue",
                    "number": it["number"],
                    "title": it["title"],
                    "url": it["html_url"],
                    "labels": [l["name"] for l in it.get("labels", [])],
                    "updated_at": it["updated_at"],
                    "body": it.get("body") or "",
                })
                if max_items and len(out) >= max_items:
                    return out
            page += 1
    return out

async def crawl_discussions(repo: str, token: str | None, since_iso: str | None, max_items: int | None):
    """
    GraphQL discussions ordered by UPDATED_AT desc. Stop when older than since_iso.
    """
    owner, name = repo.split("/", 1)
    out, cursor, has_next = [], None, True

    async with httpx.AsyncClient(headers=auth_headers(token), timeout=30.0) as client:
        while has_next:
            body = {
                "query": """
                query($owner:String!, $name:String!, $cursor:String) {
                  repository(owner:$owner, name:$name){
                    discussions(first:100, after:$cursor, orderBy:{field:UPDATED_AT, direction:DESC}) {
                      pageInfo { hasNextPage endCursor }
                      nodes { id number title url updatedAt bodyText }
                    }
                  }
                }""",
                "variables": {"owner": owner, "name": name, "cursor": cursor},
            }
            data = await _request_json(client, "POST", GRAPHQL, json=body)
            nodes = data["data"]["repository"]["discussions"]["nodes"]

            for d in nodes:
                if since_iso and isoparse(d["updatedAt"]) < isoparse(since_iso):
                    has_next = False
                    break
                out.append({
                    "id": f"D{d['id']}",
                    "type": "discussion",
                    "number": d["number"],
                    "title": d["title"],
                    "url": d["url"],
                    "labels": [],
                    "updated_at": d["updatedAt"],
                    "body": d.get("bodyText") or "",
                })
                if max_items and len(out) >= max_items:
                    return out

            info = data["data"]["repository"]["discussions"]["pageInfo"]
            cursor, has_next = info["endCursor"], info["hasNextPage"]
    return out

def build_and_write_outputs(out_dir: pathlib.Path, docs_list: list[dict]):
    # Slim docs; compute excerpt, remove body
    for d in docs_list:
        d["excerpt"] = excerpt(d.pop("body", ""), 400)

    # Metadata file
    docs_path = out_dir / "github-docs.json"
    with open(docs_path, "w", encoding="utf-8") as f:
        json.dump(docs_list, f, ensure_ascii=False)

    # Lunr index (title + excerpt + labels)
    idx = lunr(
        ref="id",
        fields=("title", "excerpt", "labels"),
        documents=[{
            "id": d["id"],
            "title": d["title"],
            "excerpt": d["excerpt"],
            "labels": " ".join(d.get("labels", [])),
        } for d in docs_list],
        languages=["en"],  # remove if you don't want stemming
    )
    with open(out_dir / "github-lunr-index.json", "w", encoding="utf-8") as f:
        json.dump(idx.serialize(), f)

def _merge_incremental(existing_path: pathlib.Path, current_map: dict[str, dict]) -> dict[str, dict]:
    if existing_path.exists():
        prev = json.load(open(existing_path, encoding="utf-8"))
        prev_map = {d["id"]: d for d in prev}
        prev_map.update(current_map)  # overwrite with latest
        return prev_map
    return current_map

async def run(repo: str, out: str, full: bool, max_items: int | None):
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("GH_TOKEN env var required (PAT or GITHUB_TOKEN)", file=sys.stderr)
        sys.exit(1)

    cache_dir = ".github-index-cache"
    state_path, state = load_state(cache_dir)
    since = None if full or not state.get("since") else state["since"]

    # Crawl concurrently
    issues_task = asyncio.create_task(crawl_issues(repo, token, since, max_items))
    disc_task   = asyncio.create_task(crawl_discussions(repo, token, since, max_items))

    issues, discussions = await asyncio.gather(issues_task, disc_task)

    # Merge + incremental carryover
    out_dir = pathlib.Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs_path = out_dir / "github-docs.json"

    current = {d["id"]: d for d in (issues + discussions)}
    all_docs_map = _merge_incremental(docs_path, current) if since else current
    docs_list = list(all_docs_map.values())

    # Write outputs
    build_and_write_outputs(out_dir, docs_list)

    # Update state (pick newest updated_at)
    latest = max((d["updated_at"] for d in docs_list), default=since or datetime.now(timezone.utc).isoformat())
    state["since"] = latest
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state_path, state)

    print(f"Indexed docs: {len(docs_list)}")
    print(f"Wrote: {docs_path} and {out_dir / 'github-lunr-index.json'}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--out", required=True, help="output dir (e.g. out/)")
    ap.add_argument("--full", action="store_true", help="ignore cached since")
    ap.add_argument("--max", type=int, default=None, help="limit total items (testing)")
    args = ap.parse_args()
    asyncio.run(run(args.repo, args.out, args.full, args.max))