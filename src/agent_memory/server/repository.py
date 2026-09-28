"""Data access — plain async functions over an `AsyncSession`. No ORM ceremony in
the routes; no HTTP in here. Each function is independently unit-testable against a
test database.

Full-text search is the one place raw Postgres shows through (`plainto_tsquery`,
`ts_headline`, `ts_rank`) — expressed with SQLAlchemy `func` rather than string SQL.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .embedding import Embedder, cosine
from .models import Memory, MemoryReview, MemoryTag, Tag
from .review import NEIGHBOUR_COUNT, TAG_COUNT, Verdict

# ts_headline markers match the old snippet() output so clients render identically.
_HEADLINE = "StartSel=→ , StopSel= ←, MaxWords=32, MinWords=1, ShortWord=0, HighlightAll=FALSE"

# What every memory read loads with it: its tags and its review, each in one
# extra query per result set, so `_dump` never triggers a lazy load.
_LOAD = (selectinload(Memory.tags), selectinload(Memory.review))

# A new memory whose vector scores this close to one already in its project is
# a duplicate. Cosine on unit vectors: 1.0 is the same text, 0.92 is a rewording.
DUPLICATE_THRESHOLD = 0.92

# Reciprocal rank fusion constant. A hit at rank r adds 1 / (RRF_K + r) to a
# memory's fused score. 60 is the value from the original paper; it keeps the
# top ranks from crushing everything below them.
RRF_K = 60


def _since_days_window(n: int) -> tuple[datetime, None]:
    """Rolling window: everything from the start of the day N days ago through now
    (0=today, 7=the past week). Upper bound stays open so today is always included."""
    day = date.today() - timedelta(days=int(n))
    return datetime.combine(day, time.min), None


def _as_dt(v):
    """Coerce a date/datetime string bound to the timestamptz column into a real
    datetime — asyncpg won't implicitly cast text to timestamp the way psycopg did."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _embed(embedder: Embedder | None, content: str) -> tuple[list[float] | None, str | None]:
    """The vector and model name to store for `content`. Both are None when there
    is no embedder or it has no model (a `NullEmbedder`), so a server without the
    model still writes memories, just without vectors."""
    if embedder is None or embedder.model_name is None:
        return None, None
    return embedder.embed([content])[0], embedder.model_name


def _dump_review(r: MemoryReview | None) -> dict | None:
    """The API view of a review row: the verdict and nothing about how it was made."""
    if r is None:
        return None
    return {"verdict": r.verdict, "rule": r.rule, "reason": r.reason,
            "rewrite": r.rewrite, "duplicate_of": r.duplicate_of,
            "tags": list(r.tags or [])}


def _dump(m: Memory, snippet: str | None = None, score: float | None = None) -> dict:
    # `embedding` and `embedding_model` stay out on purpose: they are internal.
    return {
        "id": m.id,
        "timestamp": m.timestamp.isoformat(sep=" ", timespec="seconds") if m.timestamp else None,
        "agent": m.agent,
        "project": m.project,
        "content": m.content,
        "type": m.type,
        "tags": [t.name for t in m.tags],
        "snippet": snippet,
        "score": score,
        "review": _dump_review(m.review),
    }


async def _get_or_create_tag(session: AsyncSession, name: str, description: str | None) -> Tag:
    """Reuse an existing tag (updating its descriptor only when a new one is given),
    or create it. A brand-new tag with no descriptor defaults to its own name — a
    descriptor is required in the schema but never required from the caller."""
    tag = (
        await session.execute(select(Tag).where(func.lower(Tag.name) == func.lower(name)))
    ).scalar_one_or_none()
    if tag is not None:
        if description and description != tag.description:
            tag.description = description
        return tag
    tag = Tag(name=name, description=description or name)
    session.add(tag)
    await session.flush()
    return tag


# ---- operations -----------------------------------------------------------
async def add(session, content, agent, project, tags, mtype, embedder=None) -> int:
    embedding, model = _embed(embedder, content)
    m = Memory(content=content, agent=agent, project=project, type=mtype,
               embedding=embedding, embedding_model=model)
    session.add(m)  # add before wiring tags so the back-reference resolves cleanly
    for spec in tags:
        m.tags.append(await _get_or_create_tag(session, spec.name, spec.description))
    await session.flush()
    return m.id


async def find_duplicate(session, embedder, content, project) -> tuple[int, float] | None:
    """The memory that `content` would duplicate, as `(id, score)`, or None.

    Embeds the content and compares it, in Python, with every vector in the
    same project (`project=None` compares with the memories that have none).
    Only vectors from the same model count: a vector from another model is not
    comparable, and may not even have the same length. Returns the best match
    when its cosine is at or above `DUPLICATE_THRESHOLD`. Without a model
    (no embedder, or a `NullEmbedder`) there is nothing to compare, so it
    returns None and nothing is ever refused."""
    vector, model = _embed(embedder, content)
    if vector is None:
        return None
    stmt = (
        select(Memory.id, Memory.embedding)
        .where(Memory.project.is_not_distinct_from(project))
        .where(Memory.embedding.is_not(None))
        .where(Memory.embedding_model == model)
    )
    best: tuple[int, float] | None = None
    for mid, stored in await session.execute(stmt):
        score = cosine(vector, list(stored))
        if best is None or score > best[1]:
            best = (mid, score)
    if best is not None and best[1] >= DUPLICATE_THRESHOLD:
        return best
    return None


async def query(session, *, since_days=None, since=None, until=None, project=None,
                agent=None, tag=None, mtype=None, limit=None) -> list[dict]:
    if since_days is not None:
        since, until = _since_days_window(since_days)

    stmt = select(Memory).options(*_LOAD)
    if since:
        stmt = stmt.where(Memory.timestamp >= _as_dt(since))
    if until:
        stmt = stmt.where(Memory.timestamp <= _as_dt(until))
    if project:
        stmt = stmt.where(Memory.project == project)
    if agent:
        stmt = stmt.where(Memory.agent == agent)
    if mtype:
        stmt = stmt.where(Memory.type == mtype)
    if tag:
        stmt = stmt.where(Memory.tags.any(func.lower(Tag.name) == func.lower(tag)))
    stmt = stmt.order_by(Memory.timestamp.desc())
    if limit:
        stmt = stmt.limit(int(limit))
    rows = (await session.execute(stmt)).scalars().all()
    return [_dump(m) for m in rows]


def _search_filters(stmt, *, project=None, agent=None, since=None, tag=None):
    """The WHERE clauses every search mode shares."""
    if project:
        stmt = stmt.where(Memory.project == project)
    if agent:
        stmt = stmt.where(Memory.agent == agent)
    if since:
        stmt = stmt.where(Memory.timestamp >= _as_dt(since))
    if tag:
        stmt = stmt.where(Memory.tags.any(func.lower(Tag.name) == func.lower(tag)))
    return stmt


async def search(session, text, *, project=None, agent=None, since=None, tag=None, limit=None) -> list[dict]:
    """Keyword search: rows whose words match `text`, best ts_rank first. Each
    row carries its rank as `score` and a highlighted `snippet`."""
    tsquery = func.plainto_tsquery("english", text)
    snippet = func.ts_headline("english", Memory.content, tsquery, _HEADLINE)
    rank = func.ts_rank(Memory.content_tsv, tsquery)

    stmt = (
        select(Memory, snippet.label("snippet"), rank.label("score"))
        .options(*_LOAD)
        .where(Memory.content_tsv.op("@@")(tsquery))
    )
    stmt = _search_filters(stmt, project=project, agent=agent, since=since, tag=tag)
    stmt = stmt.order_by(rank.desc())
    if limit:
        stmt = stmt.limit(int(limit))
    rows = (await session.execute(stmt)).all()
    return [_dump(m, snippet=snip, score=float(score)) for m, snip, score in rows]


async def search_semantic(session, embedder: Embedder, text, *, project=None, agent=None,
                          since=None, tag=None, limit=None) -> list[dict]:
    """Search by meaning: rows closest to `text` in vector space, best first.

    Takes the same filters as `search`, but only rows that have a vector can
    match. The whole candidate set is loaded and scored in Python with `cosine`;
    that is fine at the sizes this system holds, and it needs no extension in
    Postgres. Ties are broken by id, newest first. `limit` of 0 or None means
    all rows. Each row carries the cosine as `score`; `snippet` is None, since
    there are no matched words to highlight."""
    query_vec = embedder.embed([text])[0]

    stmt = (
        select(Memory)
        .options(*_LOAD)
        # Only vectors from the running model: another model's vector may have a
        # different width, and cosine of two widths is an error, not a low score.
        .where(Memory.embedding.is_not(None), Memory.embedding_model == embedder.model_name)
    )
    stmt = _search_filters(stmt, project=project, agent=agent, since=since, tag=tag)
    rows = (await session.execute(stmt)).scalars().all()

    scored = [(cosine(query_vec, m.embedding), m) for m in rows]
    scored.sort(key=lambda pair: (-pair[0], -pair[1].id))
    if limit:
        scored = scored[: int(limit)]
    return [_dump(m, score=score) for score, m in scored]


async def search_hybrid(session, embedder: Embedder, text, *, project=None, agent=None,
                        since=None, tag=None, limit=None) -> list[dict]:
    """Search by words and by meaning at once, fused by rank.

    Runs `search` and `search_semantic` with the same filters and no limit,
    then fuses the two lists with reciprocal rank fusion: each memory scores
    the sum, over the lists it appears in, of `1 / (RRF_K + rank)`, rank
    starting at 1. A memory found by both lists outranks one found by only
    one. Ranks, not raw scores, are fused, because ts_rank and cosine live on
    different scales and adding them would mean nothing.

    Best fused score first, ties broken by id, newest first. `limit` of 0 or
    None means all rows. `score` is the fused score; `snippet` comes from the
    keyword hit when there is one, else None."""
    filters = dict(project=project, agent=agent, since=since, tag=tag, limit=0)
    by_words = await search(session, text, **filters)
    by_meaning = await search_semantic(session, embedder, text, **filters)

    fused: dict[int, dict] = {}
    for hits in (by_words, by_meaning):
        for rank, hit in enumerate(hits, start=1):
            row = fused.get(hit["id"])
            if row is None:
                # The first list to name a memory supplies its fields. The
                # keyword list goes first, so its snippet wins when both hit.
                row = fused[hit["id"]] = dict(hit, score=0.0)
            row["score"] += 1.0 / (RRF_K + rank)
    rows = sorted(fused.values(), key=lambda r: (-r["score"], -r["id"]))
    if limit:
        rows = rows[: int(limit)]
    return rows


async def get(session, mid: int) -> dict | None:
    m = (
        await session.execute(
            select(Memory).options(*_LOAD).where(Memory.id == mid)
        )
    ).scalar_one_or_none()
    return _dump(m) if m is not None else None


async def update(session, mid, *, content=None, project=None, mtype=None,
                 set_tags=None, add_tags=None, remove_tags=None,
                 embedder=None) -> list[str] | None:
    m = (
        await session.execute(
            select(Memory).options(*_LOAD).where(Memory.id == mid)
        )
    ).scalar_one_or_none()
    if m is None:
        return None
    changes: list[str] = []

    if content is not None and content != m.content:
        m.content = content
        # New text, new vector. Without a model this clears the old one, since a
        # vector of the old text would be wrong for the new one.
        m.embedding, m.embedding_model = _embed(embedder, content)
        changes.append("content")
    if project is not None:
        m.project = project or None
        changes.append(f"project → {m.project}")
    if mtype is not None:
        m.type = mtype or None
        changes.append(f"type → {m.type}")
    if set_tags is not None:
        m.tags = [await _get_or_create_tag(session, s.name, s.description) for s in set_tags]
        names = [t.name for t in m.tags]
        changes.append(f"tags set to: {', '.join(names) if names else '(none)'}")
    if add_tags:
        have = {t.id for t in m.tags}
        added = []
        for s in add_tags:
            tag = await _get_or_create_tag(session, s.name, s.description)
            if tag.id not in have:
                m.tags.append(tag)
                have.add(tag.id)
            added.append(s.name)
        changes.append(f"+tags: {', '.join(added)}")
    if remove_tags:
        lowered = {n.lower() for n in remove_tags}
        m.tags = [t for t in m.tags if t.name.lower() not in lowered]
        changes.append(f"-tags: {', '.join(remove_tags)}")
    return changes


async def get_many(session, ids) -> list[dict]:
    if not ids:
        return []
    rows = (
        await session.execute(select(Memory).where(Memory.id.in_(list(ids))))
    ).scalars().all()
    return [
        {"id": m.id, "agent": m.agent, "project": m.project, "type": m.type, "content": m.content}
        for m in rows
    ]


async def delete(session, ids) -> None:
    await session.execute(sql_delete(Memory).where(Memory.id.in_(list(ids))))


async def list_tags(session) -> list[dict]:
    count = func.count(Memory.id)
    stmt = (
        select(Tag.name, count.label("count"), Tag.description)
        .join(Tag.memories)
        .group_by(Tag.id, Tag.name, Tag.description)
        .order_by(count.desc(), Tag.name)
    )
    return [{"name": n, "count": c, "description": d} for n, c, d in await session.execute(stmt)]


async def list_projects(session) -> list[dict]:
    count = func.count()
    stmt = (
        select(Memory.project, count.label("count"))
        .where(Memory.project.is_not(None))
        .group_by(Memory.project)
        .order_by(count.desc())
    )
    return [{"project": p, "count": c} for p, c in await session.execute(stmt)]


async def list_agents(session) -> list[dict]:
    count = func.count()
    stmt = (
        select(Memory.agent, count.label("count"))
        .group_by(Memory.agent)
        .order_by(count.desc())
    )
    return [{"agent": a, "count": c} for a, c in await session.execute(stmt)]


async def stats(session) -> dict:
    async def scalar(stmt):
        return (await session.execute(stmt)).scalar()

    return {
        "total": await scalar(select(func.count(Memory.id))),
        "agents": await scalar(select(func.count(func.distinct(Memory.agent)))),
        "projects": await scalar(
            select(func.count(func.distinct(Memory.project))).where(Memory.project.is_not(None))
        ),
        "tags": await scalar(select(func.count(Tag.id))),
        "today": await scalar(
            select(func.count(Memory.id)).where(func.date(Memory.timestamp) == func.current_date())
        ),
        "week": await scalar(
            select(func.count(Memory.id)).where(Memory.timestamp >= func.current_date() - 7)
        ),
        "oldest": await _ts(session, func.min(Memory.timestamp)),
        "newest": await _ts(session, func.max(Memory.timestamp)),
    }


async def _ts(session, agg):
    val = (await session.execute(select(agg))).scalar()
    return val.isoformat(sep=" ", timespec="seconds") if val else None


# ---- dashboard: unified list + tag management (D1) ------------------------
async def list_memories(session, *, q=None, tags=(), project=None, agent=None, mtype=None,
                        since_days=None, since=None, until=None, order="date_desc",
                        limit=100, offset=0) -> tuple[list[dict], int]:
    """The one list endpoint: full-text (`q`) + multi-tag + filters + order +
    pagination. `limit=0` means no limit. Returns (items, total) where total ignores
    limit/offset."""
    if since_days is not None:
        since, until = _since_days_window(since_days)

    conds = []
    if since:
        conds.append(Memory.timestamp >= _as_dt(since))
    if until:
        conds.append(Memory.timestamp <= _as_dt(until))
    if project:
        conds.append(Memory.project == project)
    if agent:
        conds.append(Memory.agent == agent)
    if mtype:
        conds.append(Memory.type == mtype)
    if tags:  # OR: the memory matches if it carries ANY of the listed tags
        conds.append(Memory.tags.any(func.lower(Tag.name).in_([t.lower() for t in tags])))
    tsquery = func.plainto_tsquery("english", q) if q else None
    if tsquery is not None:
        conds.append(Memory.content_tsv.op("@@")(tsquery))

    total = (await session.execute(select(func.count()).select_from(Memory).where(*conds))).scalar()

    base = select(Memory).options(*_LOAD).where(*conds)
    if tsquery is not None:
        snippet = func.ts_headline("english", Memory.content, tsquery, _HEADLINE)
        stmt = (select(Memory, snippet.label("snippet"))
                .options(*_LOAD).where(*conds)
                .order_by(func.ts_rank(Memory.content_tsv, tsquery).desc(), Memory.id.desc())
                .offset(offset))
        if limit:  # 0 = no limit
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).all()
        items = [_dump(m, snippet=s) for m, s in rows]
    else:
        # order = "<field>_<asc|desc>"; field ∈ date/agent/project/type/id.
        cols = {"date": Memory.timestamp, "agent": Memory.agent,
                "project": Memory.project, "type": Memory.type, "id": Memory.id}
        field, _, direction = (order or "date_desc").rpartition("_")
        col = cols.get(field, Memory.timestamp)
        ordered = col.asc() if direction == "asc" else col.desc()
        # Stable tiebreak on id so pages don't shuffle within equal sort keys.
        stmt = base.order_by(ordered, Memory.id.desc()).offset(offset)
        if limit:  # 0 = no limit
            stmt = stmt.limit(limit)
        items = [_dump(m) for m in (await session.execute(stmt)).scalars().all()]
    return items, total


async def _find_tag(session, name):
    return (await session.execute(
        select(Tag).where(func.lower(Tag.name) == func.lower(name)))).scalar_one_or_none()


async def _tag_count(session, tag_id) -> int:
    return (await session.execute(
        select(func.count()).select_from(MemoryTag).where(MemoryTag.tag_id == tag_id))).scalar()


async def _reassign(session, sources, target, description=None) -> int:
    """Move every memory link from each source tag to `target`, delete the sources.
    Returns the number of (memory, source) links reassigned."""
    if description:
        target.description = description
    affected = 0
    for src in sources:
        mids = (await session.execute(
            select(MemoryTag.memory_id).where(MemoryTag.tag_id == src.id))).scalars().all()
        for mid in mids:
            exists = (await session.execute(select(MemoryTag).where(
                MemoryTag.memory_id == mid, MemoryTag.tag_id == target.id))).scalar_one_or_none()
            if exists is None:
                session.add(MemoryTag(memory_id=mid, tag_id=target.id))
            affected += 1
        await session.delete(src)  # cascade drops the source's links
    await session.flush()
    return affected


async def patch_tag(session, name, *, new_name=None, description=None) -> dict | None:
    tag = await _find_tag(session, name)
    if tag is None:
        return None
    if new_name and new_name.lower() != tag.name.lower():
        collision = await _find_tag(session, new_name)
        if collision is not None:  # rename onto an existing tag = merge into it
            await _reassign(session, [tag], collision, description)
            return {"name": collision.name, "description": collision.description,
                    "count": await _tag_count(session, collision.id)}
        tag.name = new_name
    if description is not None:
        tag.description = description or tag.name
    await session.flush()
    return {"name": tag.name, "description": tag.description, "count": await _tag_count(session, tag.id)}


async def delete_tag(session, name) -> dict | None:
    tag = await _find_tag(session, name)
    if tag is None:
        return None
    affected = await _tag_count(session, tag.id)
    await session.delete(tag)  # cascade removes its links
    return {"removed": tag.name, "memories_affected": affected}


async def merge_tags(session, sources, target, description=None) -> dict:
    tgt = await _find_tag(session, target)
    if tgt is None:
        tgt = Tag(name=target, description=description or target)
        session.add(tgt)
        await session.flush()
    src_tags = []
    for s in sources:
        t = await _find_tag(session, s)
        if t is not None and t.id != tgt.id:
            src_tags.append(t)
    affected = await _reassign(session, src_tags, tgt, description)
    return {"target": tgt.name, "memories_affected": affected, "removed": [t.name for t in src_tags]}


async def detach_tag(session, name, memory_ids=None) -> dict | None:
    tag = await _find_tag(session, name)
    if tag is None:
        return None
    stmt = sql_delete(MemoryTag).where(MemoryTag.tag_id == tag.id)
    if memory_ids:
        stmt = stmt.where(MemoryTag.memory_id.in_(list(memory_ids)))
    res = await session.execute(stmt)
    return {"detached": res.rowcount}


# ---- review: the model's verdict on a memory --------------------------------
async def review_input(session, mid: int) -> tuple[dict, list[dict]] | None:
    """What the reviewer gets for memory `mid`: the memory itself and its
    nearest neighbours, as `(memory, neighbours)`. None when there is no such
    memory.

    The neighbours are the `NEIGHBOUR_COUNT` memories of the same project
    (no project matches no project) closest to it by cosine over the stored
    vectors, best first, the memory itself left out. Only vectors from the
    same model as the memory's own count, as in `find_duplicate`. A memory
    with no vector, which is what a server without the embedding model
    writes, gets no neighbours."""
    m = (await session.execute(select(Memory).options(*_LOAD).where(Memory.id == mid))
         ).scalar_one_or_none()
    if m is None:
        return None
    if m.embedding is None:
        return _dump(m), []
    stmt = (
        select(Memory)
        .options(*_LOAD)
        .where(Memory.project.is_not_distinct_from(m.project))
        .where(Memory.id != m.id)
        .where(Memory.embedding.is_not(None))
        .where(Memory.embedding_model == m.embedding_model)
    )
    rows = (await session.execute(stmt)).scalars().all()
    scored = [(cosine(m.embedding, list(other.embedding)), other) for other in rows]
    scored.sort(key=lambda pair: (-pair[0], -pair[1].id))
    return _dump(m), [_dump(other, score=score) for score, other in scored[:NEIGHBOUR_COUNT]]


# The verdicts that mark a memory as flagged: the model said no, or said
# it should be written differently. An approve is not a flag.
FLAGGED_VERDICTS = ("reject", "rewrite")


def _flagged_filters(stmt, *, project=None, verdict=None):
    """The WHERE clauses of a flagged listing: joined to the review row, kept
    to the flagged verdicts (or only `verdict`), and to `project` when given.
    A `verdict` that is not one of the flagged ones is a caller's error."""
    if verdict is not None and verdict not in FLAGGED_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(FLAGGED_VERDICTS)} (got {verdict!r})")
    wanted = FLAGGED_VERDICTS if verdict is None else (verdict,)
    stmt = stmt.join(Memory.review).where(MemoryReview.verdict.in_(wanted))
    if project:
        stmt = stmt.where(Memory.project == project)
    return stmt


async def flagged(session, *, project=None, verdict=None, limit=None) -> list[dict]:
    """The memories the review flagged: those whose review says reject or
    rewrite, or only `verdict` when given, newest review first (ties by id,
    newest first). Same shape as `query`, the review included. `project`
    narrows to one project. `limit` of 0 or None means all."""
    stmt = _flagged_filters(select(Memory).options(*_LOAD), project=project, verdict=verdict)
    stmt = stmt.order_by(MemoryReview.created_at.desc(), Memory.id.desc())
    if limit:
        stmt = stmt.limit(int(limit))
    rows = (await session.execute(stmt)).scalars().all()
    return [_dump(m) for m in rows]


async def count_flagged(session, *, project=None, verdict=None) -> int:
    """How many memories `flagged` would list with the same filters and no limit."""
    stmt = _flagged_filters(select(func.count(Memory.id)), project=project, verdict=verdict)
    return (await session.execute(stmt)).scalar()


async def without_review(session, *, limit=None) -> list[int]:
    """The ids of the memories that have no review row, newest first (ties by
    id, newest first). These are the memories written while the review model
    was off or could not answer. `limit` of 0 or None means all."""
    stmt = (
        select(Memory.id)
        .outerjoin(Memory.review)
        .where(MemoryReview.memory_id.is_(None))
        .order_by(Memory.timestamp.desc(), Memory.id.desc())
    )
    if limit:
        stmt = stmt.limit(int(limit))
    return list((await session.execute(stmt)).scalars().all())


async def set_review(session, mid: int, verdict: Verdict, model: str) -> dict:
    """Store `verdict` as the review of memory `mid`, replacing any earlier
    one, and return the API view of it. `model` names the model that gave it.
    The memory must exist; a missing one fails on the foreign key."""
    row = await session.get(MemoryReview, mid)
    if row is None:
        row = MemoryReview(memory_id=mid)
        session.add(row)
    row.verdict = verdict.verdict
    row.rule = verdict.rule
    row.reason = verdict.reason
    row.rewrite = verdict.rewrite
    row.duplicate_of = verdict.duplicate_of
    row.tags = list(verdict.tags) or None
    row.model = model
    row.created_at = datetime.now(timezone.utc)
    await session.flush()
    return _dump_review(row)


async def tags_for_review(session, embedder: Embedder | None, content: str,
                          limit: int = TAG_COUNT) -> list[str]:
    """The names of the tags the reviewer may suggest for `content`: at most
    `limit` of the tags in use, best first.

    With a model, best means closest in meaning: each tag's `name: description`
    and the content are embedded here and now (nothing is stored) and ranked
    by cosine, ties broken by name. Without a model (no embedder, or a
    `NullEmbedder`) the most used tags come first, as `list_tags` orders
    them."""
    rows = await list_tags(session)
    if not rows:
        return []
    if embedder is None or embedder.model_name is None:
        return [r["name"] for r in rows[:limit]]
    texts = [f"{r['name']}: {r['description'] or r['name']}" for r in rows]
    query_vec, *tag_vecs = embedder.embed([content, *texts])
    scored = [(cosine(query_vec, vec), r["name"]) for vec, r in zip(tag_vecs, rows)]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:limit]]


# ---- reindex: fill in missing or stale vectors -----------------------------
async def reindex(session, embedder: Embedder | None, batch: int = 64) -> int:
    """Give every row a vector from the current model.

    Picks rows with no vector, or with a vector from another model, in id
    order, `batch` rows at a time; embeds each batch with one call; writes the
    vectors back. Returns the number of rows updated. Does nothing and returns
    0 when there is no embedder or it has no model."""
    if embedder is None or embedder.model_name is None:
        return 0
    model = embedder.model_name
    stale = or_(Memory.embedding.is_(None), Memory.embedding_model.is_distinct_from(model))
    updated = 0
    last_id = 0
    while True:
        # Walk by id, not by offset: rows already done drop out of the filter,
        # so an offset would skip rows.
        stmt = (select(Memory).where(stale, Memory.id > last_id)
                .order_by(Memory.id).limit(int(batch)))
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return updated
        vectors = embedder.embed([m.content for m in rows])
        for m, vec in zip(rows, vectors):
            m.embedding, m.embedding_model = vec, model
        await session.flush()
        updated += len(rows)
        last_id = rows[-1].id
