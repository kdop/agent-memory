"""Command-line interface — formats data from the store; no SQL lives here.

`main()` is the `memory-cli` entry point (console_script + the repo-root shim).
The storage and config layers it drives now live alongside it in the package.
"""

import argparse
import sys

from .config import get_agent_name, config_path, load_config, save_config
from .store import SqliteStore, get_store

# Box-drawing separators used in the rendered output.
HBAR = "━" * 39
FOOT = "─" * 50


def _print_meta(row):
    """Print the shared 🕒/👤/🏷️ metadata lines for a memory dict."""
    print(f"🕒 {row['timestamp']}")
    print(f"👤 {row['agent']}", end="")
    if row['project']:
        print(f" @ {row['project']}", end="")
    if row['type']:
        print(f" [{row['type']}]", end="")
    print()
    if row['tags']:
        print(f"🏷️  {', '.join(row['tags'])}")


def add_memory(args, store):
    agent = args.agent or get_agent_name()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    mid = store.add(args.content, agent, args.project, tags, args.type)
    print(f"✓ Memory #{mid} added ({agent})")


def query_memories(args, store):
    rows = store.query(today=args.today, yesterday=args.yesterday, since=args.since,
                       until=args.until, project=args.project, agent=args.agent,
                       tag=args.tag, mtype=args.type, limit=args.limit)
    if not rows:
        print("No memories found.")
        return
    for row in rows:
        print(f"\n━━━ #{row['id']} {HBAR}")
        _print_meta(row)
        print(f"\n{row['content']}")
    print(f"\n{FOOT}")
    print(f"Found {len(rows)} memories")


def search_memories(args, store):
    rows = store.search(args.query, project=args.project, agent=args.agent,
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


def list_tags(args, store):
    rows = store.list_tags()
    if not rows:
        print("No tags found.")
        return
    print("🏷️  Tags:\n")
    for name, count in rows:
        print(f"  {name:<20} ({count})")


def list_projects(args, store):
    rows = store.list_projects()
    if not rows:
        print("No projects found.")
        return
    print("📂 Projects:\n")
    for project, count in rows:
        print(f"  {project:<20} ({count} memories)")


def show_memory(args, store):
    row = store.get(args.id)
    if not row:
        print(f"✗ Memory #{args.id} not found.")
        sys.exit(1)
    print(f"\n━━━ #{row['id']} {HBAR}")
    _print_meta(row)
    print(f"\n{row['content']}")


def update_memory(args, store):
    if store.get(args.id) is None:
        print(f"✗ Memory #{args.id} not found.")
        sys.exit(1)

    # Resolve content source (mutually exclusive at argparse level)
    new_content = None
    if args.content_file:
        with open(args.content_file) as f:
            new_content = f.read()
    elif args.content == "-":
        new_content = sys.stdin.read()
    elif args.content is not None:
        new_content = args.content

    changes = store.update(args.id, new_content=new_content, project=args.project,
                           mtype=args.type, set_tags=args.set_tags,
                           add_tags=args.add_tags, remove_tags=args.remove_tags)
    if not changes:
        print(f"No changes specified for memory #{args.id}. Use --help for options.")
        return
    print(f"✓ Memory #{args.id} updated: {'; '.join(changes)}")


def delete_memory(args, store):
    rows = store.get_many(args.ids)
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
        if row['project']:
            meta += f"@{row['project']}"
        if row['type']:
            meta += f"|{row['type']}"
        meta += "]"
        print(f"  #{row['id']} {meta} {preview}")

    if not args.yes:
        print("\n⚠ Dry run. Re-run with --yes to actually delete.")
        return

    store.delete(args.ids)
    print(f"\n✓ Deleted {len(rows)} {noun}.")


def show_stats(args, store):
    s = store.stats()
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
    """Get/set persistent settings (currently: db_path). No DB access."""
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


def ensure_db_or_confirm(db_path, assume_yes):
    """Guard against silently creating a DB at a wrong/typo'd path. If the resolved
    path doesn't exist: with --yes, create it; on a TTY, ask; otherwise error (#4)."""
    if db_path.exists() or assume_yes:
        return
    if not sys.stdin.isatty():
        print(f"No database at {db_path}.\n"
              f"Pass --yes to create it, or set AGENT_MEMORY_DB / `config set db_path`.",
              file=sys.stderr)
        sys.exit(1)
    answer = input(f"No DB at {db_path} — create it? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted — no database created.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Shared memory system for AI agents")
    parser.add_argument("--yes", "-y", dest="assume_yes", action="store_true",
                        help="Auto-create the database if missing; skip the confirmation prompt")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Add command
    add_parser = subparsers.add_parser("add", help="Add a memory")
    add_parser.add_argument("content", help="Memory content")
    add_parser.add_argument("--project", help="Project name")
    add_parser.add_argument("--agent", help="Agent name (auto-detected if not specified)")
    add_parser.add_argument("--tags", help="Comma-separated tags")
    add_parser.add_argument("--type", help="Memory type (decision, code, lesson, note)")
    add_parser.set_defaults(func=add_memory)

    # Query command
    query_parser = subparsers.add_parser("query", help="Query memories")
    query_parser.add_argument("--today", action="store_true", help="Today's memories")
    query_parser.add_argument("--yesterday", action="store_true", help="Yesterday's memories")
    query_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    query_parser.add_argument("--until", help="Until date (YYYY-MM-DD)")
    query_parser.add_argument("--project", help="Filter by project")
    query_parser.add_argument("--agent", help="Filter by agent")
    query_parser.add_argument("--tag", help="Filter by tag")
    query_parser.add_argument("--type", help="Filter by type")
    query_parser.add_argument("--limit", type=int, help="Limit results")
    query_parser.set_defaults(func=query_memories)

    # Search command
    search_parser = subparsers.add_parser("search", help="Full-text search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--project", help="Filter by project")
    search_parser.add_argument("--agent", help="Filter by agent")
    search_parser.add_argument("--tag", help="Filter by tag")
    search_parser.add_argument("--since", help="Since date (YYYY-MM-DD)")
    search_parser.add_argument("--limit", type=int, default=20, help="Limit results")
    search_parser.set_defaults(func=search_memories)

    # Tags command
    tags_parser = subparsers.add_parser("tags", help="List all tags")
    tags_parser.set_defaults(func=list_tags)

    # Projects command
    projects_parser = subparsers.add_parser("projects", help="List all projects")
    projects_parser.set_defaults(func=list_projects)

    # Stats command
    stats_parser = subparsers.add_parser("stats", help="Show statistics")
    stats_parser.set_defaults(func=show_stats)

    # Show command (fetch one memory in full by ID)
    show_parser = subparsers.add_parser("show", help="Show one memory by ID")
    show_parser.add_argument("id", type=int, help="Memory ID")
    show_parser.set_defaults(func=show_memory)

    # Update command
    update_parser = subparsers.add_parser("update", help="Update fields of an existing memory by ID")
    update_parser.add_argument("id", type=int, help="Memory ID")
    content_group = update_parser.add_mutually_exclusive_group()
    content_group.add_argument("--content", help="New content (use '-' to read from stdin)")
    content_group.add_argument("--content-file", help="Read new content from file")
    update_parser.add_argument("--project", help="Set project (empty string clears)")
    update_parser.add_argument("--type", help="Set type (empty string clears)")
    update_parser.add_argument("--set-tags", help="Replace all tags (comma-separated; empty string removes all)")
    update_parser.add_argument("--add-tags", help="Add tags (comma-separated; idempotent)")
    update_parser.add_argument("--remove-tags", help="Remove tags (comma-separated)")
    update_parser.set_defaults(func=update_memory)

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete one or more memories by ID")
    delete_parser.add_argument("ids", type=int, nargs="+", help="Memory ID(s) to delete")
    delete_parser.add_argument("--yes", "-y", action="store_true", help="Actually delete (without this, runs as dry-run)")
    delete_parser.set_defaults(func=delete_memory)

    # Config command (persistent settings; e.g. the DB path)
    config_parser = subparsers.add_parser("config", help="Get/set persistent settings (e.g. db_path)")
    config_sub = config_parser.add_subparsers(dest="config_action")
    cset = config_sub.add_parser("set", help="Set a setting, e.g. config set db_path /path/to/memory.db")
    cset.add_argument("key")
    cset.add_argument("value")
    cget = config_sub.add_parser("get", help="Show all settings, or one key")
    cget.add_argument("key", nargs="?")
    config_sub.add_parser("path", help="Print the config file location")

    args = parser.parse_args()

    # `config` manages settings and must not touch (or create) a database.
    if args.command == "config":
        config_command(args, config_parser)
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    store = get_store()
    # The missing-DB guard is a local-file concern; a remote ApiStore has none.
    if isinstance(store, SqliteStore):
        ensure_db_or_confirm(store.db_path, args.assume_yes)
    for msg in store.initialize():
        print(msg)

    args.func(args, store)


if __name__ == "__main__":
    main()
