"""Command-line interface — a thin presentation layer over the HTTP API.

`memory-cli` talks to the FastAPI service through `ApiClient`; it never touches a
database directly. `main()` is the console-script entry point (+ the repo-root shim).
Only `config` (local settings) stays offline.
"""

import argparse
import json
import sys

from .client import ApiClient, ApiRefused, ApiUnreachable, DuplicateMemory, ReviewRefused
from .config import config_path, get_agent_name, load_config, save_config

# Box-drawing separators used in the rendered output.
HBAR = "━" * 39
FOOT = "─" * 50


def _parse_tags_json(raw, flag):
    """Parse a --tags/--set-tags/--add-tags value: a JSON array of tag objects,
    e.g. '[{"name":"auth","description":"authentication flow"},{"name":"db"}]'.
    description is optional (a new tag with none defaults to its own name). Exits
    with a clear error on malformed input rather than raising."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"✗ {flag} must be a JSON array of tag objects, "
              f"e.g. '[{{\"name\":\"auth\",\"description\":\"authentication flow\"}}]' ({e})")
        sys.exit(1)
    if not isinstance(parsed, list) or not all(isinstance(t, dict) and t.get("name") for t in parsed):
        print(f"✗ {flag} must be a JSON array of objects with at least a \"name\" field")
        sys.exit(1)
    return parsed


def _print_meta(row):
    """Print the shared 🕒/👤/🏷️ metadata lines for a memory dict."""
    print(f"🕒 {row['timestamp']}")
    print(f"👤 {row['agent']}", end="")
    if row.get('project'):
        print(f" @ {row['project']}", end="")
    if row.get('type'):
        print(f" [{row['type']}]", end="")
    print()
    if row.get('tags'):
        print(f"🏷️  {', '.join(row['tags'])}")
    _print_review(row)


def _verdict_head(verdict, rule, duplicate_of):
    """`reject, rule 2`, or `reject, duplicate of #7` for a repeat, or just
    `approve`: the verdict part of a review line."""
    head = verdict
    if rule is not None:
        head += f", rule {rule}"
    if duplicate_of is not None:
        head += f", duplicate of #{duplicate_of}"
    return head


def _print_suggestion(rewrite, tags):
    """The suggested text, indented under `suggested:`, and one
    `suggested tags: a, b` line when the model named any."""
    if rewrite:
        print("suggested:")
        for line in rewrite.splitlines():
            print(f"    {line}")
    if tags:
        print(f"suggested tags: {', '.join(tags)}")


def _print_review(row):
    """One line with the review model's verdict, when the server has one:
    `review: reject, rule 2: <reason>`. A repeat names the memory it repeats
    instead of a rule. A rewrite is followed by the suggested text, indented
    under `suggested:`, and by `suggested tags: a, b` when the model named
    any. Nothing is applied: the memory stays as it was written. Nothing is
    printed for a memory that has no review."""
    review = row.get("review")
    if not review:
        return
    head = _verdict_head(review["verdict"], review.get("rule"), review.get("duplicate_of"))
    print(f"review: {head}: {review.get('reason', '')}")
    if review["verdict"] == "rewrite":
        _print_suggestion(review.get("rewrite"), review.get("tags"))


def add_memory(args, client):
    agent = args.agent or get_agent_name()
    tags = _parse_tags_json(args.tags, "--tags") or []
    try:
        mid, warnings = client.add_with_warnings(args.content, agent, args.project, tags,
                                                 args.type, force=args.force)
    except DuplicateMemory as e:
        # Nothing was stored. Exit 3 so a script can tell this apart from an error.
        print(f"✗ Duplicate of memory #{e.existing_id} (score {e.score:.2f}). "
              f"Use 'memory update {e.existing_id}' or --force.")
        sys.exit(3)
    except ReviewRefused as e:
        # The review model said no, and nothing was stored. The suggestion is
        # printed the way `show` prints a stored one. Exit 4: 3 is a duplicate.
        print(f"✗ Review: {_verdict_head(e.verdict, e.rule, e.duplicate_of)}: {e.explanation}")
        if e.verdict == "rewrite":
            _print_suggestion(e.rewrite, e.tags)
        print("Fix the entry, or pass --force to store it as written.")
        sys.exit(4)
    print(f"✓ Memory #{mid} added ({agent})")
    # One line per rule the entry breaks. The memory is stored either way.
    for w in warnings:
        print(f"warning: {w}")


def _effective_limit(args):
    """--all wins over --limit; limit 0 means no limit on the server."""
    return 0 if getattr(args, "all", False) else args.limit


def _print_listing(rows, total, noun="memories"):
    """Print a list of memories the way `query` does: one block per memory
    with its meta lines (the review line included) and its content, then a
    footer that says how many were shown out of `total`."""
    for row in rows:
        print(f"\n━━━ #{row['id']} {HBAR}")
        _print_meta(row)
        print(f"\n{row['content']}")
    print(f"\n{FOOT}")
    if total > len(rows):
        print(f"Found {len(rows)} of {total} {noun} (use --all or --limit to see more)")
    else:
        print(f"Found {len(rows)} {noun}")


def query_memories(args, client):
    rows, total = client.query_with_total(
        since_days=args.since_days, since=args.since, until=args.until,
        project=args.project, agent=args.agent, tag=args.tag,
        mtype=args.type, limit=_effective_limit(args))
    if not rows:
        print("No memories found.")
        return
    _print_listing(rows, total)


def review_memories(args, client):
    """`memory review`: list the memories the review flagged. With
    `--missing`, ask the server to review the memories that have no
    review yet instead."""
    if args.missing:
        if args.project or args.verdict or args.all:
            print("✗ --missing goes with --limit only, not with --project, --verdict or --all.")
            sys.exit(2)
        n = client.review_missing(limit=args.limit)
        print(f"✓ Scheduled {n} reviews")
        return
    rows, total = client.flagged_with_total(
        project=args.project, verdict=args.verdict, limit=_effective_limit(args))
    if not rows:
        print("No flagged memories.")
        return
    _print_listing(rows, total, noun="flagged memories")


def _search_header(row):
    """The `━━━ #id ━━━…` line of a search result, with the score when the
    server sent one: `━━━ #12 score 0.87 ━━━…`."""
    score = row.get("score")
    if score is None:
        return f"━━━ #{row['id']} {HBAR}"
    return f"━━━ #{row['id']} score {score:.2f} {HBAR}"


def search_memories(args, client):
    limit = _effective_limit(args)
    rows = client.search(args.query, project=args.project, agent=args.agent,
                         since=args.since, tag=args.tag, limit=limit, mode=args.mode)
    if not rows:
        print(f"No memories found for: {args.query}")
        return
    print(f"🔍 Search results for: {args.query}\n")
    for row in rows:
        print(_search_header(row))
        _print_meta(row)
        # Keyword search sends a snippet with the matches marked. Semantic
        # search has no words to mark, so it sends none: show the content.
        body = row.get("snippet")
        if body is None:
            body = row.get("content", "")
        print(f"\n{body}\n")
    print(f"{FOOT}")
    if limit and len(rows) >= limit:
        print(f"Found {len(rows)} matches (limit reached; use --all or --limit to see more)")
    else:
        print(f"Found {len(rows)} matches")


def list_tags(args, client):
    rows = client.list_tags()
    if not rows:
        print("No tags found.")
        return
    print("🏷️  Tags:\n")
    for t in rows:
        print(f"  {t['name']:<20} ({t['count']})")
        if t.get('description') and t['description'] != t['name']:
            print(f"       ↳ {t['description']}")


def list_projects(args, client):
    rows = client.list_projects()
    if not rows:
        print("No projects found.")
        return
    print("📂 Projects:\n")
    for p in rows:
        print(f"  {p['project']:<20} ({p['count']} memories)")


def show_memory(args, client):
    row = client.get(args.id)
    if not row:
        print(f"✗ Memory #{args.id} not found.")
        sys.exit(1)
    print(f"\n━━━ #{row['id']} {HBAR}")
    _print_meta(row)
    print(f"\n{row['content']}")


def update_memory(args, client):
    if client.get(args.id) is None:
        print(f"✗ Memory #{args.id} not found.")
        sys.exit(1)

    new_content = None
    if args.content_file:
        with open(args.content_file) as f:
            new_content = f.read()
    elif args.content == "-":
        new_content = sys.stdin.read()
    elif args.content is not None:
        new_content = args.content

    set_tags = _parse_tags_json(args.set_tags, "--set-tags")
    add_tags = _parse_tags_json(args.add_tags, "--add-tags")
    remove_tags = [t.strip() for t in args.remove_tags.split(",") if t.strip()] if args.remove_tags else None

    changes = client.update(args.id, content=new_content, project=args.project,
                            mtype=args.type, set_tags=set_tags,
                            add_tags=add_tags, remove_tags=remove_tags)
    if not changes:
        print(f"No changes specified for memory #{args.id}. Use --help for options.")
        return
    print(f"✓ Memory #{args.id} updated: {'; '.join(changes)}")


def delete_memory(args, client):
    rows = client.get_many(args.ids)
    if not rows:
        print("No memories found with the given IDs.")
        sys.exit(1)

    found_ids = {row["id"] for row in rows}
    missing = [i for i in args.ids if i not in found_ids]
    if missing:
        print(f"⚠ Not found: {', '.join(str(i) for i in missing)}")

    noun = "memory" if len(rows) == 1 else "memories"
    print(f"\n{'Will delete' if not args.yes else 'Deleting'} {len(rows)} {noun}:\n")
    for row in rows:
        preview = row["content"].replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:100] + "..."
        meta = f"[{row['agent']}"
        if row.get('project'):
            meta += f"@{row['project']}"
        if row.get('type'):
            meta += f"|{row['type']}"
        meta += "]"
        print(f"  #{row['id']} {meta} {preview}")

    if not args.yes:
        print("\n⚠ Dry run. Re-run with --yes to actually delete.")
        return

    result = client.delete(args.ids)
    print(f"\n✓ Deleted {result['deleted']} {noun}.")


def show_stats(args, client):
    s = client.stats()
    print("📊 Memory Statistics\n")
    print(f"Total memories:    {s['total']}")
    print(f"Agents:            {s['agents']}")
    print(f"Projects:          {s['projects']}")
    print(f"Tags:              {s['tags']}")
    print(f"\nToday:             {s['today']}")
    print(f"Last 7 days:       {s['week']}")
    print(f"\nOldest:            {s['oldest'] or 'N/A'}")
    print(f"Newest:            {s['newest'] or 'N/A'}")


def reindex_memories(args, client):
    n = client.reindex()
    print(f"✓ Reindexed {n} memories")


def config_command(args, parser):
    """Get/set persistent client settings (api_url, api_token, server_host/port).
    Local-only; never touches the API."""
    if args.config_action == "set":
        cfg = load_config()
        cfg[args.key] = args.value
        save_config(cfg)
        print(f"✓ Set {args.key} = {args.value}")
    elif args.config_action == "get":
        cfg = load_config()
        if args.key:
            print(cfg[args.key] if args.key in cfg else f"(unset) {args.key}")
        elif not cfg:
            print("No settings configured.")
        else:
            for k, v in cfg.items():
                print(f"{k} = {v}")
    elif args.config_action == "path":
        print(config_path())
    else:
        parser.print_help()
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Shared memory system for AI agents (API client)")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    add_parser = subparsers.add_parser("add", help="Add a memory")
    add_parser.add_argument("content", help="Memory content")
    add_parser.add_argument("--project", help="Project name")
    add_parser.add_argument("--agent", help="Agent name (auto-detected if not specified)")
    add_parser.add_argument(
        "--tags",
        help='JSON array of tag objects, e.g. \'[{"name":"auth","description":"authentication flow"},{"name":"db"}]\'. '
             "description is optional (a new tag with none defaults to its own name).")
    add_parser.add_argument("--type", help="Memory type (decision, lesson, note, preference)")
    add_parser.add_argument("--force", action="store_true",
                            help="Store even when a near-duplicate exists in the project "
                                 "(without it, a duplicate is refused with exit code 3), "
                                 "and even when the server's review model would refuse "
                                 "the entry (exit code 4)")
    add_parser.set_defaults(func=add_memory)

    query_parser = subparsers.add_parser("query", help="Query memories")
    query_parser.add_argument("--since-days", type=int, help="Rolling window: memories since the start of the day N days ago (0=today, 7=past week)")
    query_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    query_parser.add_argument("--until", help="Until date (YYYY-MM-DD)")
    query_parser.add_argument("--project", help="Filter by project")
    query_parser.add_argument("--agent", help="Filter by agent")
    query_parser.add_argument("--tag", help="Filter by tag")
    query_parser.add_argument("--type", help="Filter by type")
    query_parser.add_argument("--limit", type=int, help="Limit results (default 100; 0 = all)")
    query_parser.add_argument("--all", action="store_true", help="Return every match (same as --limit 0)")
    query_parser.set_defaults(func=query_memories)

    search_parser = subparsers.add_parser(
        "search", help="Search memories by words, by meaning, or both",
        formatter_class=argparse.RawTextHelpFormatter)
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "--mode", choices=["keyword", "semantic", "hybrid"], default="keyword",
        help="How to match (default keyword):\n"
             "  keyword   matches words\n"
             "  semantic  matches meaning; needs the embedding model on the server\n"
             "  hybrid    combines both")
    search_parser.add_argument("--project", help="Filter by project")
    search_parser.add_argument("--agent", help="Filter by agent")
    search_parser.add_argument("--tag", help="Filter by tag")
    search_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    search_parser.add_argument("--limit", type=int, default=20, help="Limit results (default 20; 0 = all)")
    search_parser.add_argument("--all", action="store_true", help="Return every match (same as --limit 0)")
    search_parser.set_defaults(func=search_memories)

    tags_parser = subparsers.add_parser("tags", help="List all tags")
    tags_parser.set_defaults(func=list_tags)

    projects_parser = subparsers.add_parser("projects", help="List all projects")
    projects_parser.set_defaults(func=list_projects)

    stats_parser = subparsers.add_parser("stats", help="Show statistics")
    stats_parser.set_defaults(func=show_stats)

    show_parser = subparsers.add_parser("show", help="Show one memory by ID")
    show_parser.add_argument("id", type=int, help="Memory ID")
    show_parser.set_defaults(func=show_memory)

    update_parser = subparsers.add_parser("update", help="Update fields of an existing memory by ID")
    update_parser.add_argument("id", type=int, help="Memory ID")
    content_group = update_parser.add_mutually_exclusive_group()
    content_group.add_argument("--content", help="New content (use '-' to read from stdin)")
    content_group.add_argument("--content-file", help="Read new content from file")
    update_parser.add_argument("--project", help="Set project (empty string clears)")
    update_parser.add_argument("--type", help="Set type (empty string clears)")
    update_parser.add_argument("--set-tags", help='Replace all tags: JSON array of tag objects, e.g. \'[{"name":"auth"}]\' ("[]" removes all)')
    update_parser.add_argument("--add-tags", help='Add tags (idempotent): JSON array of tag objects, e.g. \'[{"name":"db","description":"the database"}]\'')
    update_parser.add_argument("--remove-tags", help="Remove tags by name (comma-separated)")
    update_parser.set_defaults(func=update_memory)

    delete_parser = subparsers.add_parser("delete", help="Delete one or more memories by ID")
    delete_parser.add_argument("ids", type=int, nargs="+", help="Memory ID(s) to delete")
    delete_parser.add_argument("--yes", "-y", action="store_true", help="Actually delete (without this, runs as dry-run)")
    delete_parser.set_defaults(func=delete_memory)

    reindex_parser = subparsers.add_parser(
        "reindex", help="Give every memory a vector from the server's current embedding model")
    reindex_parser.set_defaults(func=reindex_memories)

    review_parser = subparsers.add_parser(
        "review",
        help="List the memories the review model flagged (reject or rewrite), newest first",
        description="List the memories the review model flagged: those whose verdict is "
                    "reject or rewrite, newest review first, each with its verdict and "
                    "reason. With --missing, ask the server to review the memories that "
                    "have no verdict yet instead (written while the model was off).")
    review_parser.add_argument("--project", help="Only this project")
    review_parser.add_argument("--verdict", choices=["reject", "rewrite"],
                               help="Only this verdict (default: both)")
    review_parser.add_argument(
        "--limit", type=int,
        help="How many to show (default 100; 0 = all). With --missing: how many "
             "memories to review (default 50; 0 = all)")
    review_parser.add_argument("--all", action="store_true",
                               help="Show every flagged memory (same as --limit 0)")
    review_parser.add_argument(
        "--missing", action="store_true",
        help="Do not list; ask the server to review the memories that have no verdict "
             "yet, newest first. Runs in the background; prints how many were scheduled")
    review_parser.set_defaults(func=review_memories)

    config_parser = subparsers.add_parser("config", help="Get/set persistent client settings (api_url, api_token)")
    config_sub = config_parser.add_subparsers(dest="config_action")
    cset = config_sub.add_parser("set", help="Set a setting, e.g. config set api_url http://host:8099")
    cset.add_argument("key")
    cset.add_argument("value")
    cget = config_sub.add_parser("get", help="Show all settings, or one key")
    cget.add_argument("key", nargs="?")
    config_sub.add_parser("path", help="Print the config file location")

    args = parser.parse_args()

    # `config` manages local settings and must not touch the API.
    if args.command == "config":
        config_command(args, config_parser)
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args, ApiClient())
    except (ApiUnreachable, ApiRefused) as e:
        # No server, or the server said no and said why: show the message,
        # never a traceback.
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
