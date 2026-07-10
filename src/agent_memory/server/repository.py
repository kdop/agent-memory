"""Data access — plain async functions over an `AsyncSession`. No ORM ceremony in
the routes; no HTTP in here. Each function is independently unit-testable against a
test database.

Full-text search is the one place raw Postgres shows through (`plainto_tsquery`,
`ts_headline`, `ts_rank`) — expressed with SQLAlchemy `func` rather than string SQL.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import Memory, MemoryTag, Tag

# ts_headline markers match the old snippet() output so clients render identically.
_HEADLINE = "StartSel=→ , StopSel= ←, MaxWords=32, MinWords=1, ShortWord=0, HighlightAll=FALSE"


def _since_days_window(n: int) -> tuple[datetime, datetime]:
    """A single calendar day N days ago (0=today, 1=yesterday) as (since, until)."""
    day = date.today() - timedelta(days=int(n))
    return datetime.combine(day, time.min), datetime.combine(day, time(23, 59, 59))


def _as_dt(v):
    """Coerce a date/datetime string bound to the timestamptz column into a real
    datetime — asyncpg won't implicitly cast text to timestamp the way psycopg did."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _dump(m: Memory, snippet: str | None = None) -> dict:
    return {
        "id": m.id,
        "timestamp": m.timestamp.isoformat(sep=" ", timespec="seconds") if m.timestamp else None,
        "agent": m.agent,
        "project": m.project,
        "content": m.content,
        "type": m.type,
        "tags": [t.name for t in m.tags],
        "snippet": snippet,
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
async def add(session, content, agent, project, tags, mtype) -> int:
    m = Memory(content=content, agent=agent, project=project, type=mtype)
    session.add(m)  # add before wiring tags so the back-reference resolves cleanly
    for spec in tags:
        m.tags.append(await _get_or_create_tag(session, spec.name, spec.description))
    await session.flush()
    return m.id


async def query(session, *, since_days=None, since=None, until=None, project=None,
                agent=None, tag=None, mtype=None, limit=None) -> list[dict]:
    if since_days is not None:
        since, until = _since_days_window(since_days)

    stmt = select(Memory).options(selectinload(Memory.tags))
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


async def search(session, text, *, project=None, agent=None, since=None, tag=None, limit=None) -> list[dict]:
    tsquery = func.plainto_tsquery("english", text)
    snippet = func.ts_headline("english", Memory.content, tsquery, _HEADLINE)
    rank = func.ts_rank(Memory.content_tsv, tsquery)

    stmt = (
        select(Memory, snippet.label("snippet"))
        .options(selectinload(Memory.tags))
        .where(Memory.content_tsv.op("@@")(tsquery))
    )
    if project:
        stmt = stmt.where(Memory.project == project)
    if agent:
        stmt = stmt.where(Memory.agent == agent)
    if since:
        stmt = stmt.where(Memory.timestamp >= _as_dt(since))
    if tag:
        stmt = stmt.where(Memory.tags.any(func.lower(Tag.name) == func.lower(tag)))
    stmt = stmt.order_by(rank.desc())
    if limit:
        stmt = stmt.limit(int(limit))
    rows = (await session.execute(stmt)).all()
    return [_dump(m, snippet=snip) for m, snip in rows]


async def get(session, mid: int) -> dict | None:
    m = (
        await session.execute(
            select(Memory).options(selectinload(Memory.tags)).where(Memory.id == mid)
        )
    ).scalar_one_or_none()
    return _dump(m) if m is not None else None


async def update(session, mid, *, content=None, project=None, mtype=None,
                 set_tags=None, add_tags=None, remove_tags=None) -> list[str] | None:
    m = (
        await session.execute(
            select(Memory).options(selectinload(Memory.tags)).where(Memory.id == mid)
        )
    ).scalar_one_or_none()
    if m is None:
        return None
    changes: list[str] = []

    if content is not None and content != m.content:
        m.content = content
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
    """The dashboard's one list endpoint: full-text (`q`) + AND multi-tag + filters +
    order + pagination. Returns (items, total) where total ignores limit/offset."""
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

    base = select(Memory).options(selectinload(Memory.tags)).where(*conds)
    if tsquery is not None:
        snippet = func.ts_headline("english", Memory.content, tsquery, _HEADLINE)
        stmt = (select(Memory, snippet.label("snippet"))
                .options(selectinload(Memory.tags)).where(*conds)
                .order_by(func.ts_rank(Memory.content_tsv, tsquery).desc(), Memory.id.desc())
                .limit(limit).offset(offset))
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
        stmt = base.order_by(ordered, Memory.id.desc()).limit(limit).offset(offset)
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
