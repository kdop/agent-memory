#!/usr/bin/env python3
"""Replay the write checks over past memories and print what they would have done.

Before a refusal goes live we want to see what it would have refused. This script
walks the memories of a database copy in the order they were written and runs the
two write checks on each one as if it were new: the duplicate check against the
memories written before it in the same project, and the warnings from
`server/checks.py`.

    python scripts/replay_write_checks.py --dsn postgresql://memory:memory@127.0.0.1:5434/memory
    python scripts/replay_write_checks.py --dsn ... --since 2026-09-01 --until 2026-09-30
    python scripts/replay_write_checks.py --dsn ... --embedder fake

One line per memory in the range:

    <id>  <date>  <project or ->  <refuse #<existing id> <score> | ok>  <warnings or ->

then a blank line and the totals: how many would be refused, how many broke each
warning rule, how many were clean.

The database is only read. Vectors are computed on the fly for every memory up to
the end of the range (the ones before `--since` too, since the check would have
compared against them), so nothing depends on the vectors stored in the rows, and
nothing is written back. The DSN must point at localhost: this runs against the
copy from `scripts/db_copy.sh`, never against the live database. It takes the
target from `--dsn` only and never looks at AGENT_MEMORY_DB.

`--embedder model` (the default) uses the same model the server would, through
`make_embedder()`, so it needs the `[embed]` extra. `--embedder fake` uses a small
hash-based embedder: same text gives the same vector, different text gives an
unrelated one. It is only good for checking the script itself.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from agent_memory.server.checks import RULES, warnings_for
from agent_memory.server.db import resolve_async_dsn
from agent_memory.server.embedding import Embedder, cosine, make_embedder
from agent_memory.server.models import Memory
from agent_memory.server.repository import DUPLICATE_THRESHOLD
from agent_memory.server.schemas import MemoryIn, TagIn

LOCAL_HOSTS = {"localhost", "127.0.0.1"}

# How many texts go to the embedder at once.
BATCH = 64


class FakeEmbedder(Embedder):
    """Deterministic vectors from a hash of the text, like the one the tests use.
    Same text, same vector; unit length, 8 wide. Two texts that differ by one
    character get unrelated vectors, so only exact copies score as duplicates."""

    model_name = "fake"
    dim = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, s: str) -> list[float]:
        digest = hashlib.sha256(s.encode("utf-8")).digest()
        raw = [(b - 128) / 128.0 for b in digest[: self.dim]]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


def is_local(dsn: str) -> bool:
    try:
        return urlsplit(dsn).hostname in LOCAL_HOSTS
    except ValueError:
        return False


def build_embedder(kind: str) -> Embedder:
    if kind == "fake":
        return FakeEmbedder()
    embedder = make_embedder()
    if embedder.model_name is None:
        sys.exit("no embedding model: install the [embed] extra and leave "
                 "AGENT_MEMORY_EMBED_MODEL unset, or pass --embedder fake")
    return embedder


def memory_in(m: Memory) -> MemoryIn:
    """The request the memory would have been written with. A type the API no
    longer accepts is treated as no type: the warnings only care whether it is a
    decision or a lesson."""
    tags = [TagIn(name=t.name, description=t.description) for t in m.tags]
    try:
        return MemoryIn(content=m.content, project=m.project, agent=m.agent,
                        tags=tags, type=m.type)
    except ValidationError:
        return MemoryIn(content=m.content, project=m.project, agent=m.agent,
                        tags=tags, type=None)


def local_day_start(d: date) -> datetime:
    """Midnight at the start of `d` in the local time zone, as an aware datetime,
    so the range lines up with the dates the report prints."""
    return datetime.combine(d, time.min).astimezone()


async def load_memories(session: AsyncSession, until: date | None) -> list[Memory]:
    """Every memory up to the end of `until` (all of them when it is None), in the
    order they were written: timestamp first, id to break ties."""
    stmt = select(Memory).options(selectinload(Memory.tags))
    if until is not None:
        stmt = stmt.where(Memory.timestamp < local_day_start(until + timedelta(days=1)))
    stmt = stmt.order_by(Memory.timestamp, Memory.id)
    return list((await session.execute(stmt)).scalars())


def embed_all(embedder: Embedder, memories: list[Memory]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(memories), BATCH):
        vectors.extend(embedder.embed([m.content for m in memories[i:i + BATCH]]))
    return vectors


def replay(memories: list[Memory], vectors: list[list[float]], since: date | None):
    """Walk the memories in order and yield `(memory, refusal, warnings)` for each
    one in the range. `refusal` is `(existing id, score)` or None."""
    start = local_day_start(since) if since is not None else None
    seen: dict[str | None, list[tuple[int, list[float]]]] = defaultdict(list)
    for m, vec in zip(memories, vectors):
        best = None
        for other_id, other_vec in seen[m.project]:
            score = cosine(vec, other_vec)
            if best is None or score > best[1]:
                best = (other_id, score)
        seen[m.project].append((m.id, vec))
        if start is not None and m.timestamp < start:
            continue
        refusal = best if best is not None and best[1] >= DUPLICATE_THRESHOLD else None
        yield m, refusal, warnings_for(memory_in(m))


def report(rows) -> str:
    lines = []
    refused = clean = 0
    warned = Counter()
    for m, refusal, warnings in rows:
        verdict = f"refuse #{refusal[0]} {refusal[1]:.2f}" if refusal else "ok"
        lines.append(f"{m.id}  {m.timestamp.astimezone().date()}  {m.project or '-'}  "
                     f"{verdict}  {','.join(warnings) or '-'}")
        if refusal is not None:
            refused += 1
        elif not warnings:
            clean += 1
        warned.update(warnings)
    per_rule = ", ".join(f"{name} {warned[name]}" for name, _ in RULES)
    lines.append("")
    lines.append(f"totals: {len(lines) - 1} memories, {refused} refused, "
                 f"warned ({per_rule}), {clean} clean")
    return "\n".join(lines)


async def run(dsn: str, since: date | None, until: date | None, embedder: Embedder) -> str:
    engine = create_async_engine(resolve_async_dsn(dsn), poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            # Belt and braces: the session is never committed, and Postgres
            # refuses any write inside this transaction anyway.
            await session.execute(text("SET TRANSACTION READ ONLY"))
            memories = await load_memories(session, until)
    finally:
        await engine.dispose()
    vectors = embed_all(embedder, memories)
    return report(replay(memories, vectors, since))


def main():
    ap = argparse.ArgumentParser(
        description="Replay the write checks over the memories of a database copy.")
    ap.add_argument("--dsn", required=True,
                    help="the copy's DSN; the host must be localhost or 127.0.0.1")
    ap.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="first day to report (the check still sees earlier memories)")
    ap.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="last day to report, inclusive")
    ap.add_argument("--embedder", choices=["fake", "model"], default="model",
                    help="'model' is what the server uses; 'fake' is for testing the script")
    args = ap.parse_args()

    if not is_local(args.dsn):
        print("refusing: this script runs only against a copy of the database "
              "(the DSN host must be localhost or 127.0.0.1)", file=sys.stderr)
        sys.exit(2)

    embedder = build_embedder(args.embedder)
    print(asyncio.run(run(args.dsn, args.since, args.until, embedder)))


if __name__ == "__main__":
    main()
