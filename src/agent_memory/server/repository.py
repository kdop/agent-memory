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

from .models import Memory, Tag

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
        .outerjoin(Tag.memories)
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
