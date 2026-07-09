"""Command-line interface — a thin presentation layer over the HTTP API.

`memory-cli` talks to the FastAPI service through `ApiClient`; it never touches a
database directly. `main()` is the console-script entry point (+ the repo-root shim).
Only `config` (local settings) stays offline.
"""

import argparse
import json
import sys

from .client import ApiClient, ApiUnreachable
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


def add_memory(args, client):
    agent = args.agent or get_agent_name()
    tags = _parse_tags_json(args.tags, "--tags") or []
    mid = client.add(args.content, agent, args.project, tags, args.type)
    print(f"✓ Memory #{mid} added ({agent})")


def query_memories(args, client):
    rows = client.query(since_days=args.since_days, since=args.since, until=args.until,
                        project=args.project, agent=args.agent, tag=args.tag,
                        mtype=args.type, limit=args.limit)
    if not rows:
        print("No memories found.")
        return
    for row in rows:
        print(f"\n━━━ #{row['id']} {HBAR}")
        _print_meta(row)
        print(f"\n{row['content']}")
    print(f"\n{FOOT}")
    print(f"Found {len(rows)} memories")


def search_memories(args, client):
    rows = client.search(args.query, project=args.project, agent=args.agent,
                         since=args.since, tag=args.tag, limit=args.limit)
    if not rows:
        print(f"No memories found for: {args.query}")
        return
    print(f"🔍 Search results for: {args.query}\n")
    for row in rows:
        print(f"━━━ #{row['id']} {HBAR}")
        _print_meta(row)
        print(f"\n{row['snippet']}\n")
    print(f"{FOOT}")
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
    add_parser.add_argument("--type", help="Memory type (decision, code, lesson, note)")
    add_parser.set_defaults(func=add_memory)

    query_parser = subparsers.add_parser("query", help="Query memories")
    query_parser.add_argument("--since-days", type=int, help="Memories from a single day N days ago (0=today, 1=yesterday)")
    query_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    query_parser.add_argument("--until", help="Until date (YYYY-MM-DD)")
    query_parser.add_argument("--project", help="Filter by project")
    query_parser.add_argument("--agent", help="Filter by agent")
    query_parser.add_argument("--tag", help="Filter by tag")
    query_parser.add_argument("--type", help="Filter by type")
    query_parser.add_argument("--limit", type=int, help="Limit results")
    query_parser.set_defaults(func=query_memories)

    search_parser = subparsers.add_parser("search", help="Full-text search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--project", help="Filter by project")
    search_parser.add_argument("--agent", help="Filter by agent")
    search_parser.add_argument("--tag", help="Filter by tag")
    search_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    search_parser.add_argument("--limit", type=int, default=20, help="Limit results")
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
    except ApiUnreachable as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
