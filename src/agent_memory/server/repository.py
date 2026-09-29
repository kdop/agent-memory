"""Data access — plain async functions over an `AsyncSession`. No ORM ceremony in
the routes; no HTTP in here. Each function is independently unit-testable against a
test database.

Full-text search is the one place raw Postgres shows through (`plainto_tsquery`,
`ts_headline`, `ts_rank`) — expressed with SQLAlchemy `func` rather than string SQL.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from .embedding import Embedder, cosine
from .models import Memory, MemoryReview, MemoryTag, Tag, TagReview
from .review import (
    FLAGGED,
    NEIGHBOUR_COUNT,
    STATUSES,
    TAG_COUNT,
    UNVERIFIED,
    VERIFIED,
    Verdict,
    status_for,
)
from . import tag_review as tr

# ts_headline markers match the old snippet() output so clients render identically.
_HEADLINE = "StartSel=→ , StopSel= ←, MaxWords=32, MinWords=1, ShortWord=0, HighlightAll=FALSE"

# What every memory read loads with it: its tags, its reviews (newest first;
# a read shows the first) and the memories that supersede it, each in one
# extra query per result set, so `_dump` never triggers a lazy load.
_LOAD = (selectinload(Memory.tags), selectinload(Memory.reviews),
         selectinload(Memory.superseded_by_rows))

# A new memory whose vector scores this close to one already in its project is
# a duplicate. Cosine on unit vectors: 1.0 is the same text, 0.92 is a rewording.
DUPLICATE_THRESHOLD = 0.92

# A new tag whose vector (`name: description`) scores this close to an
# existing tag's is the same tag under another name, and the existing one is
# used instead.
TAG_REUSE_THRESHOLD = 0.90

# Reciprocal rank fusion constant. A hit at rank r adds 1 / (RRF_K + r) to a
# memory's fused score. 60 is the value from the original paper; it keeps the
# top ranks from crushing everything below them.
RRF_K = 60

# The rules whose reject archives the memory: 2, a diary line, and 4, what
# git already holds or should hold. Neither is worth keeping in view, and a
# restore brings one back when the model was wrong.
ARCHIVE_RULES = (2, 4)


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


async def _embed_many(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Every call to the model goes through here, in a worker thread. The
    model takes tens of milliseconds per text and hundreds per batch, all
    of it CPU; on the event loop that time would stall every other request
    (a `/health` call sat for a second during the catch-up before this)."""
    return await asyncio.to_thread(embedder.embed, texts)


async def _embed(embedder: Embedder | None, content: str) -> tuple[list[float] | None, str | None]:
    """The vector and model name to store for `content`. Both are None when there
    is no embedder or it has no model (a `NullEmbedder`), so a server without the
    model still writes memories, just without vectors."""
    if embedder is None or embedder.model_name is None:
        return None, None
    return (await _embed_many(embedder, [content]))[0], embedder.model_name


def _tag_text(tag: Tag) -> str:
    """What a tag's vector is made of: its name and its description in one line."""
    return f"{tag.name}: {tag.description}"


async def _embed_tag(tag: Tag, embedder: Embedder | None) -> None:
    """Store the vector of `_tag_text(tag)` on the tag. Without a model the
    tag keeps what it has (None on a new tag): reindex fills it later."""
    vector, model = await _embed(embedder, _tag_text(tag))
    if vector is not None:
        tag.embedding, tag.embedding_model = vector, model


def _dump_review(r: MemoryReview | None, supersedes: int | None = None) -> dict | None:
    """The API view of a review row: the verdict and nothing about how it was
    made. `supersedes` is the memory's own link, which the verdict set."""
    if r is None:
        return None
    return {"verdict": r.verdict, "rule": r.rule, "reason": r.reason,
            "rewrite": r.rewrite, "duplicate_of": r.duplicate_of,
            "tags": list(r.tags or []), "supersedes": supersedes}


def _dump_history(r: MemoryReview) -> dict:
    """One entry of a memory's review history: the verdict and when it was
    given. No `supersedes`: the link lives on the memory and follows the
    newest verdict, so an older entry has none to show."""
    return {"created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
            "verdict": r.verdict, "rule": r.rule, "reason": r.reason,
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
        "review": _dump_review(m.review, m.supersedes),
        "review_status": m.review_status,
        "supersedes": m.supersedes,
        # The newest memory that supersedes this one; the rows come newest first.
        "superseded_by": m.superseded_by_rows[0].id if m.superseded_by_rows else None,
        "archived_at": (m.archived_at.isoformat(sep=" ", timespec="seconds")
                        if m.archived_at else None),
    }


def _archived_cond(archived: bool = False):
    """The WHERE clause that keeps a read to the live memories, or with
    `archived=True` to the archived ones only. Every listing, search and
    count goes through it, so an archived memory shows only where it is
    asked for; a read by id (`get`) does not, and still returns it."""
    return Memory.archived_at.is_not(None) if archived else Memory.archived_at.is_(None)


def _current_only(stmt):
    """Keep to the memories nothing supersedes: the `current=True` filter
    on a query or search. A superseded memory stays in the timeline; this
    only hides it from a read that asked for the current state."""
    return stmt.where(~Memory.superseded_by_rows.any())


def _check_status(status: str | None) -> None:
    """A `status` filter must be one of `STATUSES` (or None for no filter);
    anything else is a caller's error."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)} (got {status!r})")


async def _get_or_create_tag(session: AsyncSession, name: str, description: str | None,
                             embedder: Embedder | None = None) -> Tag:
    """Reuse an existing tag (updating its descriptor only when a new one is given),
    or create it. A brand-new tag with no descriptor defaults to its own name — a
    descriptor is required in the schema but never required from the caller.
    A new tag, or a new descriptor, gets its vector here (see `_embed_tag`)."""
    tag = (
        await session.execute(select(Tag).where(func.lower(Tag.name) == func.lower(name)))
    ).scalar_one_or_none()
    if tag is not None:
        if description and description != tag.description:
            tag.description = description
            await _embed_tag(tag, embedder)
        return tag
    tag = Tag(name=name, description=description or name)
    await _embed_tag(tag, embedder)
    session.add(tag)
    await session.flush()
    return tag


def clean_tag_name(name: str) -> str:
    """The spelling a tag is looked up and stored under: lowercase, spaces and
    underscores become hyphens, runs of hyphens become one, no hyphen at
    either end. "Code Review" and "code_review" both become "code-review"."""
    name = re.sub(r"[\s_]+", "-", name.strip().lower())
    return re.sub(r"-{2,}", "-", name).strip("-")


def _plural_forms(name: str) -> set[str]:
    """The other number of `name`: "skill" gives "skills" and "skilles",
    "skills" gives "skill" and "skill" (from "-es"). A simple rule on
    purpose; it only matters when a tag of that name already exists."""
    forms = {name + "s", name + "es"}
    if name.endswith("es"):
        forms.add(name[:-2])
    if name.endswith("s"):
        forms.add(name[:-1])
    forms.discard("")
    forms.discard(name)
    return forms


async def _resolve_tag(session: AsyncSession, spec, embedder: Embedder | None,
                       notes: list[str] | None) -> Tag:
    """The tag a memory gets for `spec` (a `TagIn`) on add or update.

    The name is cleaned first (`clean_tag_name`). Then, in order: a tag with
    that name (its description is updated as before); a tag with the plural
    or singular of it; a tag whose vector scores `TAG_REUSE_THRESHOLD` or
    more against the new tag's, from the same model; else a new tag under the
    clean name. Without a model only the name rules apply. Existing tags are
    never renamed. When the stored name is not the one written, a line saying
    so goes into `notes`, so the writer sees it."""
    name = clean_tag_name(spec.name) or spec.name.strip()
    tags = {clean_tag_name(t.name): t for t in (await session.execute(select(Tag))).scalars()}
    tag = tags.get(name)
    if tag is not None:
        if spec.description and spec.description != tag.description:
            tag.description = spec.description
            await _embed_tag(tag, embedder)
    else:
        tag = next((tags[f] for f in sorted(_plural_forms(name)) if f in tags), None)
    new = None
    if tag is None:
        new = Tag(name=name, description=spec.description or name)
        await _embed_tag(new, embedder)
        if new.embedding is not None:
            best, best_score = None, TAG_REUSE_THRESHOLD
            for t in tags.values():
                if t.embedding is None or t.embedding_model != new.embedding_model:
                    continue
                score = cosine(new.embedding, list(t.embedding))
                if score >= best_score:
                    best, best_score = t, score
            tag = best
    if tag is None:
        tag = new
        session.add(tag)
        await session.flush()
    if notes is not None and tag.name != spec.name:
        notes.append(f'tag "{spec.name}" stored as "{tag.name}"')
    return tag


# ---- operations -----------------------------------------------------------
async def add(session, content, agent, project, tags, mtype, embedder=None,
              notes: list[str] | None = None) -> int:
    """Store a memory and return its id. Tags go through `_resolve_tag`; a
    tag stored under another name than written adds a line to `notes`."""
    embedding, model = await _embed(embedder, content)
    # The tags first: resolving one reads the tags table, and the new memory
    # must not be flushed half-wired by that read.
    wired: list[Tag] = []
    for spec in tags:
        tag = await _resolve_tag(session, spec, embedder, notes)
        if tag not in wired:  # "skill" and "skills" in one add: one link
            wired.append(tag)
    m = Memory(content=content, agent=agent, project=project, type=mtype,
               embedding=embedding, embedding_model=model, tags=wired)
    session.add(m)
    await session.flush()
    return m.id


def _candidates(model: str | None):
    """The first step of every search by meaning: the id and vector of each
    row that can be scored, and nothing else. Only rows that have a vector,
    and only vectors from `model`: another model's vector may have a
    different width, and cosine of two widths is an error, not a low score.
    Callers add their own filters."""
    return (
        select(Memory.id, Memory.embedding)
        .where(Memory.embedding.is_not(None), Memory.embedding_model == model)
    )


def _reference(model: str | None, project: str | None):
    """The candidates the review paths compare with: the verified memories
    of `project` (no project matches no project) with a vector from `model`.
    A memory the model has not checked, or has flagged, is never used as
    reference, and neither is an archived one."""
    return (
        _candidates(model)
        .where(Memory.project.is_not_distinct_from(project))
        .where(Memory.review_status == VERIFIED)
        .where(_archived_cond())
    )


async def _score(session, stmt, vector, k) -> list[tuple[int, float]]:
    """Run `stmt`, a `_candidates` statement, score every row against
    `vector` with `cosine` in Python, and return the best `k` as
    `(id, score)`, best first, ties broken by id, newest first. `k` of 0 or
    None means all. Only ids and vectors cross the wire: scoring needs
    nothing else, and loading whole rows for every candidate would move
    the tags and text of the whole table for each search."""
    scored = [(mid, cosine(vector, list(stored))) for mid, stored in await session.execute(stmt)]
    scored.sort(key=lambda pair: (-pair[1], -pair[0]))
    if k:
        scored = scored[: int(k)]
    return scored


async def _scored_ids(session, embedder: Embedder, text, filters, k) -> list[tuple[int, float]]:
    """The search-by-meaning first step: embed `text`, take the candidates
    that pass the `_search_filters` in `filters`, and return the best `k`
    as `(id, score)` (see `_score`)."""
    query_vec = (await _embed_many(embedder, [text]))[0]
    stmt = _search_filters(_candidates(embedder.model_name), **filters)
    return await _score(session, stmt, query_vec, k)


async def _load_scored(session, scored) -> list[dict]:
    """The second step: load the rows named in `scored` (a `_score` result)
    with their tags and reviews in one query, and return them in that same
    order, each with its score."""
    if not scored:
        return []
    ids = [mid for mid, _ in scored]
    rows = (await session.execute(select(Memory).options(*_LOAD).where(Memory.id.in_(ids)))
            ).scalars().all()
    by_id = {m.id: m for m in rows}
    return [_dump(by_id[mid], score=score) for mid, score in scored if mid in by_id]


async def find_duplicate(session, embedder, content, project) -> tuple[int, float] | None:
    """The memory that `content` would duplicate, as `(id, score)`, or None.

    Embeds the content and compares it, in Python, with the vector of every
    verified memory in the same project (`project=None` compares with the
    memories that have none). A memory the model has not checked, or has
    flagged, is never used as reference: only `review_status = 'verified'`
    rows count. Only vectors from the same model count too: a vector from
    another model is not comparable, and may not even have the same length.
    Returns the best match when its cosine is at or above
    `DUPLICATE_THRESHOLD`. Without a model (no embedder, or a `NullEmbedder`)
    there is nothing to compare, so it returns None and nothing is ever
    refused."""
    vector, model = await _embed(embedder, content)
    if vector is None:
        return None
    # Only the best id and score are needed, so this is the one query.
    best = await _score(session, _reference(model, project), vector, 1)
    if best and best[0][1] >= DUPLICATE_THRESHOLD:
        return best[0]
    return None


async def query(session, *, since_days=None, since=None, until=None, project=None,
                agent=None, tag=None, mtype=None, status=None, current=False,
                archived=False, limit=None) -> list[dict]:
    """The timeline, newest first, narrowed by the filters. `status` keeps to
    one review status (`unverified`, `verified` or `flagged`); `current`
    hides the memories a newer one supersedes. Archived memories are left
    out; `archived=True` lists only them."""
    _check_status(status)
    if since_days is not None:
        since, until = _since_days_window(since_days)

    stmt = select(Memory).options(*_LOAD).where(_archived_cond(archived))
    if status:
        stmt = stmt.where(Memory.review_status == status)
    if current:
        stmt = _current_only(stmt)
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


def _search_filters(stmt, *, project=None, agent=None, since=None, tag=None, current=False,
                    archived=False):
    """The WHERE clauses every search mode shares. `current` hides the
    memories a newer one supersedes. Archived memories are left out;
    `archived=True` keeps to them."""
    stmt = stmt.where(_archived_cond(archived))
    if current:
        stmt = _current_only(stmt)
    if project:
        stmt = stmt.where(Memory.project == project)
    if agent:
        stmt = stmt.where(Memory.agent == agent)
    if since:
        stmt = stmt.where(Memory.timestamp >= _as_dt(since))
    if tag:
        stmt = stmt.where(Memory.tags.any(func.lower(Tag.name) == func.lower(tag)))
    return stmt


async def search(session, text, *, project=None, agent=None, since=None, tag=None,
                 current=False, archived=False, limit=None) -> list[dict]:
    """Keyword search: rows whose words match `text`, best ts_rank first. Each
    row carries its rank as `score` and a highlighted `snippet`. `current`
    hides the memories a newer one supersedes."""
    tsquery = func.plainto_tsquery("english", text)
    snippet = func.ts_headline("english", Memory.content, tsquery, _HEADLINE)
    rank = func.ts_rank(Memory.content_tsv, tsquery)

    stmt = (
        select(Memory, snippet.label("snippet"), rank.label("score"))
        .options(*_LOAD)
        .where(Memory.content_tsv.op("@@")(tsquery))
    )
    stmt = _search_filters(stmt, project=project, agent=agent, since=since, tag=tag,
                           current=current, archived=archived)
    stmt = stmt.order_by(rank.desc())
    if limit:
        stmt = stmt.limit(int(limit))
    rows = (await session.execute(stmt)).all()
    return [_dump(m, snippet=snip, score=float(score)) for m, snip, score in rows]


async def search_semantic(session, embedder: Embedder, text, *, project=None, agent=None,
                          since=None, tag=None, current=False, archived=False,
                          limit=None) -> list[dict]:
    """Search by meaning: rows closest to `text` in vector space, best first.

    Takes the same filters as `search`, but only rows that have a vector can
    match. Two steps: the id and vector of every candidate are read and
    scored in Python with `cosine` (no extension in Postgres needed), then
    only the winning rows are loaded with their tags and reviews. Ties are
    broken by id, newest first. `limit` of 0 or None means all rows. Each
    row carries the cosine as `score`; `snippet` is None, since there are no
    matched words to highlight."""
    filters = dict(project=project, agent=agent, since=since, tag=tag, current=current,
                   archived=archived)
    scored = await _scored_ids(session, embedder, text, filters, limit)
    return await _load_scored(session, scored)


async def search_hybrid(session, embedder: Embedder, text, *, project=None, agent=None,
                        since=None, tag=None, current=False, archived=False,
                        limit=None) -> list[dict]:
    """Search by words and by meaning at once, fused by rank.

    Runs `search` and the scoring step of `search_semantic` with the same
    filters and no limit, then fuses the two lists with reciprocal rank
    fusion: each memory scores the sum, over the lists it appears in, of
    `1 / (RRF_K + rank)`, rank starting at 1. A memory found by both lists
    outranks one found by only one. Ranks, not raw scores, are fused,
    because ts_rank and cosine live on different scales and adding them
    would mean nothing. The rows that only meaning found are loaded after
    the fusion, and only the ones within `limit`.

    Best fused score first, ties broken by id, newest first. `limit` of 0 or
    None means all rows. `score` is the fused score; `snippet` comes from the
    keyword hit when there is one, else None."""
    filters = dict(project=project, agent=agent, since=since, tag=tag, current=current,
                   archived=archived)
    by_words = await search(session, text, **filters, limit=0)
    by_meaning = await _scored_ids(session, embedder, text, filters, 0)

    fused: dict[int, float] = {}
    for ids in ([h["id"] for h in by_words], [mid for mid, _ in by_meaning]):
        for rank, mid in enumerate(ids, start=1):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank)
    order = sorted(fused, key=lambda mid: (-fused[mid], -mid))
    if limit:
        order = order[: int(limit)]

    # The keyword hit supplies a memory's fields when there is one, so its
    # snippet wins when both lists hit; the rest are loaded now, in one query.
    rows = {h["id"]: h for h in by_words}
    loaded = await _load_scored(session, [(mid, None) for mid in order if mid not in rows])
    rows.update((h["id"], h) for h in loaded)
    return [dict(rows[mid], score=fused[mid]) for mid in order if mid in rows]


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
        m.embedding, m.embedding_model = await _embed(embedder, content)
        # New text, new check too: every verdict was about the old text. The
        # memory goes back to unverified, and its review rows and its
        # `supersedes` link (which the newest verdict set) go with it, so the
        # next catch-up reads the new text and sets the link again if it
        # still holds. A change of tags or project alone leaves all three as
        # they are.
        m.review_status = UNVERIFIED
        m.reviews.clear()
        m.supersedes = None
        # An archived memory whose text is fixed is live again: the verdict
        # that archived it was about the old text, and the catch-up never
        # picks an archived memory, so it would stay unverified for good.
        m.archived_at = None
        changes.append("content")
    if project is not None:
        m.project = project or None
        changes.append(f"project → {m.project}")
    if mtype is not None:
        m.type = mtype or None
        changes.append(f"type → {m.type}")
    notes: list[str] = []
    if set_tags is not None:
        new_tags = []
        for s in set_tags:
            tag = await _resolve_tag(session, s, embedder, notes)
            if tag not in new_tags:
                new_tags.append(tag)
        m.tags = new_tags
        names = [t.name for t in m.tags]
        changes.append(f"tags set to: {', '.join(names) if names else '(none)'}")
    if add_tags:
        have = {t.id for t in m.tags}
        added = []
        for s in add_tags:
            tag = await _resolve_tag(session, s, embedder, notes)
            if tag.id not in have:
                m.tags.append(tag)
                have.add(tag.id)
            added.append(tag.name)
        changes.append(f"+tags: {', '.join(added)}")
    if remove_tags:
        lowered = {n.lower() for n in remove_tags}
        m.tags = [t for t in m.tags if t.name.lower() not in lowered]
        changes.append(f"-tags: {', '.join(remove_tags)}")
    return changes + notes


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
    """The tags in use by a live memory, most used first, each with its
    count, description and review status."""
    count = func.count(Memory.id)
    stmt = (
        select(Tag.name, count.label("count"), Tag.description, Tag.review_status)
        .join(Tag.memories)
        .where(_archived_cond())
        .group_by(Tag.id, Tag.name, Tag.description, Tag.review_status)
        .order_by(count.desc(), Tag.name)
    )
    return [{"name": n, "count": c, "description": d, "review_status": st}
            for n, c, d, st in await session.execute(stmt)]


async def list_projects(session) -> list[dict]:
    count = func.count()
    stmt = (
        select(Memory.project, count.label("count"))
        .where(Memory.project.is_not(None), _archived_cond())
        .group_by(Memory.project)
        .order_by(count.desc())
    )
    return [{"project": p, "count": c} for p, c in await session.execute(stmt)]


async def list_agents(session) -> list[dict]:
    count = func.count()
    stmt = (
        select(Memory.agent, count.label("count"))
        .where(_archived_cond())
        .group_by(Memory.agent)
        .order_by(count.desc())
    )
    return [{"agent": a, "count": c} for a, c in await session.execute(stmt)]


async def stats(session) -> dict:
    """The counts of the live memories; archived ones count only in
    `archived`."""
    live = _archived_cond()

    async def scalar(stmt):
        return (await session.execute(stmt)).scalar()

    return {
        "total": await scalar(select(func.count(Memory.id)).where(live)),
        "agents": await scalar(select(func.count(func.distinct(Memory.agent))).where(live)),
        "projects": await scalar(
            select(func.count(func.distinct(Memory.project)))
            .where(Memory.project.is_not(None), live)
        ),
        "tags": await scalar(select(func.count(Tag.id))),
        "today": await scalar(
            select(func.count(Memory.id))
            .where(func.date(Memory.timestamp) == func.current_date(), live)
        ),
        "week": await scalar(
            select(func.count(Memory.id)).where(Memory.timestamp >= func.current_date() - 7, live)
        ),
        "oldest": await _ts(session, func.min(Memory.timestamp)),
        "newest": await _ts(session, func.max(Memory.timestamp)),
        "archived": await scalar(select(func.count(Memory.id)).where(_archived_cond(True))),
    }


async def _ts(session, agg):
    val = (await session.execute(select(agg).where(_archived_cond()))).scalar()
    return val.isoformat(sep=" ", timespec="seconds") if val else None


# ---- dashboard: unified list + tag management (D1) ------------------------
async def list_memories(session, *, q=None, tags=(), project=None, agent=None, mtype=None,
                        status=None, current=False, archived=False, since_days=None,
                        since=None, until=None, order="date_desc", limit=100,
                        offset=0) -> tuple[list[dict], int]:
    """The one list endpoint: full-text (`q`) + multi-tag + filters + order +
    pagination. `status` keeps to one review status; `current` hides the
    memories a newer one supersedes. Archived memories are left out;
    `archived=True` lists only them, and `order="archived_desc"` sorts them
    by when they were archived. `limit=0` means no limit.
    Returns (items, total) where total ignores limit/offset."""
    _check_status(status)
    if since_days is not None:
        since, until = _since_days_window(since_days)

    conds = [_archived_cond(archived)]
    if status:
        conds.append(Memory.review_status == status)
    if current:
        conds.append(~Memory.superseded_by_rows.any())
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
        # order = "<field>_<asc|desc>"; field ∈ date/agent/project/type/id/archived.
        cols = {"date": Memory.timestamp, "agent": Memory.agent,
                "project": Memory.project, "type": Memory.type, "id": Memory.id,
                "archived": Memory.archived_at}
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


async def _reassign(session, sources, target, description=None, embedder=None) -> int:
    """Move every memory link from each source tag to `target`, delete the sources.
    A new `description` on the target gets a new vector. Returns the number of
    (memory, source) links reassigned."""
    if description and description != target.description:
        target.description = description
        await _embed_tag(target, embedder)
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


async def patch_tag(session, name, *, new_name=None, description=None,
                    embedder=None) -> dict | None:
    """Rename a tag, change its description, or both. A new description gets
    a new vector; a rename alone keeps the one it has."""
    tag = await _find_tag(session, name)
    if tag is None:
        return None
    if new_name and new_name.lower() != tag.name.lower():
        collision = await _find_tag(session, new_name)
        if collision is not None:  # rename onto an existing tag = merge into it
            await _reassign(session, [tag], collision, description, embedder)
            return {"name": collision.name, "description": collision.description,
                    "count": await _tag_count(session, collision.id)}
        tag.name = new_name
    if description is not None and (description or tag.name) != tag.description:
        tag.description = description or tag.name
        await _embed_tag(tag, embedder)
    await session.flush()
    return {"name": tag.name, "description": tag.description, "count": await _tag_count(session, tag.id)}


async def delete_tag(session, name) -> dict | None:
    tag = await _find_tag(session, name)
    if tag is None:
        return None
    affected = await _tag_count(session, tag.id)
    await session.delete(tag)  # cascade removes its links
    return {"removed": tag.name, "memories_affected": affected}


async def merge_tags(session, sources, target, description=None, embedder=None) -> dict:
    tgt = await _find_tag(session, target)
    if tgt is None:
        tgt = await _get_or_create_tag(session, target, description, embedder)
    src_tags = []
    for s in sources:
        t = await _find_tag(session, s)
        if t is not None and t.id != tgt.id:
            src_tags.append(t)
    affected = await _reassign(session, src_tags, tgt, description, embedder)
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

    The neighbours are the `NEIGHBOUR_COUNT` verified memories of the same
    project (no project matches no project) closest to it by cosine over the
    stored vectors, best first, the memory itself left out. As in
    `find_duplicate`, only `review_status = 'verified'` rows serve as
    reference, and only vectors from the same model as the memory's own
    count. A memory with no vector, which is what a server without the
    embedding model writes, gets no neighbours."""
    m = (await session.execute(select(Memory).options(*_LOAD).where(Memory.id == mid))
         ).scalar_one_or_none()
    if m is None:
        return None
    if m.embedding is None:
        return _dump(m), []
    neighbours = await _nearest(session, list(m.embedding), m.embedding_model, m.project,
                                exclude_id=m.id)
    return _dump(m), neighbours


async def neighbours_for(session, embedder: Embedder | None, content: str,
                         project: str | None) -> list[dict]:
    """The neighbours `review_input` would give a memory with this `content`
    in this `project`, before it is stored: the refuse mode reviews the
    entry first and writes it only when the model approves. The content is
    embedded here and now, and the same rules apply: same project (no
    project matches no project), verified memories only, same model, best
    first, at most `NEIGHBOUR_COUNT`. Without a model there are no
    neighbours."""
    vector, model = await _embed(embedder, content)
    if vector is None:
        return []
    return await _nearest(session, vector, model, project)


async def _nearest(session, vector, model, project, exclude_id=None) -> list[dict]:
    """The `NEIGHBOUR_COUNT` verified memories of `project` closest to
    `vector` by cosine, best first (ties: newest first), each with its
    `score`. The reference set is the same as in `find_duplicate`: only rows
    with `review_status = 'verified'`, and only vectors from `model`. An
    unverified or flagged memory is never handed to the model as reference.
    `exclude_id` leaves one memory out: the one being reviewed, when it is
    stored."""
    stmt = _reference(model, project)
    if exclude_id is not None:
        stmt = stmt.where(Memory.id != exclude_id)
    scored = await _score(session, stmt, vector, NEIGHBOUR_COUNT)
    return await _load_scored(session, scored)


# The verdicts that mark a memory as flagged: the model said no, or said
# it should be written differently. An approve is not a flag.
FLAGGED_VERDICTS = ("reject", "rewrite")


def _newest_reviews():
    """The newest review row of each memory, as a `MemoryReview` alias to
    join on. One `DISTINCT ON (memory_id)` over the table, newest first by
    `created_at`, then `id`. The one place a query looks past the memory's
    own `review_status` into the rows: everything that reads a verdict from
    the table joins this, so no listing ever sees an older one."""
    newest = (
        select(MemoryReview)
        .distinct(MemoryReview.memory_id)
        .order_by(MemoryReview.memory_id, MemoryReview.created_at.desc(), MemoryReview.id.desc())
        .subquery("newest_reviews")
    )
    return aliased(MemoryReview, newest)


def _flagged_filters(stmt, *, project=None, verdict=None, status=None, archived=False):
    """The WHERE clauses of a review listing, and the alias of the review row
    they join (see `_newest_reviews`), as `(stmt, review)`. Without `status`:
    joined to the newest review row and kept to the flagged verdicts (or only
    `verdict`). With `status`: kept to the memories with that review status
    (an outer join, so unverified memories, which have no row, are listed
    too), and to `verdict` when given. `project` narrows either. A `verdict`
    that is not one of the flagged ones, or a `status` that is not one of
    `STATUSES`, is a caller's error. Archived memories are left out;
    `archived=True` keeps to them."""
    if verdict is not None and verdict not in FLAGGED_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(FLAGGED_VERDICTS)} (got {verdict!r})")
    _check_status(status)
    review = _newest_reviews()
    on = review.memory_id == Memory.id
    stmt = stmt.where(_archived_cond(archived))
    if status is None:
        wanted = FLAGGED_VERDICTS if verdict is None else (verdict,)
        stmt = stmt.join(review, on).where(review.verdict.in_(wanted))
    else:
        stmt = stmt.outerjoin(review, on).where(Memory.review_status == status)
        if verdict is not None:
            stmt = stmt.where(review.verdict == verdict)
    if project:
        stmt = stmt.where(Memory.project == project)
    return stmt, review


async def flagged(session, *, project=None, verdict=None, status=None, archived=False,
                  limit=None) -> list[dict]:
    """The memories the review flagged: those whose newest review says reject
    or rewrite, or only `verdict` when given, newest review first (ties:
    newest memory first, then by id). With `status`, the memories with that
    review status instead (`unverified` ones have no review, so they come
    newest memory first). Same shape as `query`, the review included.
    `project` narrows to one project. Archived memories are left out;
    `archived=True` keeps to them. `limit` of 0 or None means all."""
    stmt, review = _flagged_filters(select(Memory).options(*_LOAD), project=project,
                                    verdict=verdict, status=status, archived=archived)
    stmt = stmt.order_by(review.created_at.desc().nulls_last(),
                         Memory.timestamp.desc(), Memory.id.desc())
    if limit:
        stmt = stmt.limit(int(limit))
    rows = (await session.execute(stmt)).scalars().all()
    return [_dump(m) for m in rows]


async def count_flagged(session, *, project=None, verdict=None, status=None,
                        archived=False) -> int:
    """How many memories `flagged` would list with the same filters and no limit."""
    stmt, _ = _flagged_filters(select(func.count(Memory.id)), project=project,
                               verdict=verdict, status=status, archived=archived)
    return (await session.execute(stmt)).scalar()


async def unverified_ids(session, *, limit=None) -> list[int]:
    """The ids of the unverified memories, oldest first (by timestamp, ties
    by id): the memories the model has not checked yet, written while it was
    off or when it gave no answer. This is the order the catch-up reviews
    them in, so that each one is verified before the next is compared.
    An archived memory is never picked. `limit` of 0 or None means all."""
    stmt = (
        select(Memory.id)
        .where(Memory.review_status == UNVERIFIED, _archived_cond())
        .order_by(Memory.timestamp.asc(), Memory.id.asc())
    )
    if limit:
        stmt = stmt.limit(int(limit))
    return list((await session.execute(stmt)).scalars().all())


async def set_review(session, mid: int, verdict: Verdict, model: str) -> dict:
    """Store `verdict` as a new review row of memory `mid`, keeping every
    earlier one (a verdict is part of the timeline and is never rewritten),
    set the memory's `review_status` from it (`verified` for an approve,
    `flagged` for a reject or rewrite) and its `supersedes` link from
    `verdict.supersedes` (None clears an earlier link), and return the API
    view of the review. Both follow the newest row, which this one now is.
    `model` names the model that gave it.

    Two verdicts also archive a memory (see `archive`). A reject under one
    of `ARCHIVE_RULES` archives the memory itself. A merge, a rewrite with
    `duplicate_of`, archives that older memory, and the newer one gets its
    `supersedes` link pointing at it when the verdict set none. A reversal,
    an approve with `supersedes`, archives nothing: both stay in view.

    Raises `LookupError` when there is no such memory, or when the verdict
    names a memory to supersede or to merge into that does not exist, and
    `ValueError` when it names the memory itself."""
    memory = await session.get(Memory, mid)
    if memory is None:
        raise LookupError(f"Memory #{mid} not found")
    merge = verdict.duplicate_of if verdict.verdict == "rewrite" else None
    merged_into = None
    for other in (verdict.supersedes, merge):
        if other is None:
            continue
        if other == mid:
            raise ValueError(f"Memory #{mid} cannot supersede itself")
        found = await session.get(Memory, other)
        if found is None:
            raise LookupError(f"Memory #{other} not found")
        if other == merge:
            merged_into = found
    memory.review_status = status_for(verdict.verdict)
    memory.supersedes = verdict.supersedes
    if merged_into is not None:
        merged_into.archived_at = merged_into.archived_at or _now()
        if memory.supersedes is None:
            memory.supersedes = merged_into.id
    if verdict.verdict == "reject" and verdict.rule in ARCHIVE_RULES:
        memory.archived_at = memory.archived_at or _now()
    row = MemoryReview(memory_id=mid, verdict=verdict.verdict, rule=verdict.rule,
                       reason=verdict.reason, rewrite=verdict.rewrite,
                       duplicate_of=verdict.duplicate_of, tags=list(verdict.tags) or None,
                       model=model, created_at=datetime.now(timezone.utc))
    session.add(row)
    if "reviews" not in inspect(memory).unloaded:
        # The memory's rows are loaded in this session: put the new one in
        # front, where the newest goes, so a read here shows it without
        # going back to the database.
        memory.reviews.insert(0, row)
    await session.flush()
    return _dump_review(row, memory.supersedes)


async def reviews(session, mid: int) -> list[dict] | None:
    """Every review row of memory `mid`, newest first: the memory's review
    history, each entry as `_dump_history` gives it. None when there is no
    such memory; an empty list when it has not been reviewed."""
    if await session.get(Memory, mid) is None:
        return None
    stmt = (
        select(MemoryReview)
        .where(MemoryReview.memory_id == mid)
        .order_by(MemoryReview.created_at.desc(), MemoryReview.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_dump_history(r) for r in rows]


async def tags_for_review(session, embedder: Embedder | None, content: str,
                          limit: int = TAG_COUNT) -> list[str]:
    """The names of the tags the reviewer may suggest for `content`: at most
    `limit` of the tags in use, best first.

    With a model, best means closest in meaning: the content is embedded
    here (one call, in a thread) and ranked by cosine against the vector
    each tag stores for its `name: description`, ties broken by name. A tag
    with no vector, or one from another model, is left out until reindex
    fills it. Without a model (no embedder, or a `NullEmbedder`) the most
    used tags come first, as `list_tags` orders them."""
    count = func.count(Memory.id)
    stmt = (
        select(Tag.name, Tag.embedding, Tag.embedding_model)
        .join(Tag.memories)
        .where(_archived_cond())
        .group_by(Tag.id)
        .order_by(count.desc(), Tag.name)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    if embedder is None or embedder.model_name is None:
        return [name for name, _, _ in rows[:limit]]
    query_vec = (await _embed_many(embedder, [content]))[0]
    model = embedder.model_name
    scored = [(cosine(query_vec, list(vec)), name) for name, vec, stored_model in rows
              if vec is not None and stored_model == model]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:limit]]


# ---- archive: what the review put out of view --------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


async def archive(session, mid: int) -> bool | None:
    """Archive memory `mid`: stamp `archived_at` with now, which hides it
    from every listing, search and count, and from the reference set. True
    when it was archived now, False when it already was (the first stamp
    stays, so the days left do not start over), None when there is no
    such memory."""
    memory = await session.get(Memory, mid)
    if memory is None:
        return None
    if memory.archived_at is not None:
        return False
    memory.archived_at = _now()
    await session.flush()
    return True


async def restore(session, mid: int) -> bool | None:
    """Bring memory `mid` back into view: clear `archived_at`. Its status
    and its reviews stay as they are. True when it was archived, False when
    it was not (nothing changes), None when there is no such memory."""
    memory = await session.get(Memory, mid)
    if memory is None:
        return None
    if memory.archived_at is None:
        return False
    memory.archived_at = None
    await session.flush()
    return True


async def delete_archived(session, days: float, now: datetime | None = None) -> int:
    """Delete the memories archived more than `days` days before `now`
    (the current time when None), with their tags links and reviews, and
    return how many went. `days` of 0 or less deletes nothing: an archive
    kept for good."""
    if days <= 0:
        return 0
    cutoff = (now or _now()) - timedelta(days=days)
    res = await session.execute(
        sql_delete(Memory).where(Memory.archived_at.is_not(None), Memory.archived_at < cutoff))
    return res.rowcount or 0


# ---- reindex: fill in missing or stale vectors -----------------------------
async def reindex(session, embedder: Embedder | None, batch: int = 64) -> dict:
    """Give every memory, and every tag, a vector from the current model.

    Picks rows with no vector, or with a vector from another model, in id
    order, `batch` rows at a time; embeds each batch with one call, in a
    thread; writes the vectors back. Memories first, then tags. Returns
    `{"updated": memories, "tags": tags}`, the counts of rows changed. Does
    nothing, and returns zeros, when there is no embedder or it has no
    model."""
    if embedder is None or embedder.model_name is None:
        return {"updated": 0, "tags": 0}
    model = embedder.model_name

    async def fill(table, text_of) -> int:
        stale = or_(table.embedding.is_(None), table.embedding_model.is_distinct_from(model))
        updated = 0
        last_id = 0
        while True:
            # Walk by id, not by offset: rows already done drop out of the
            # filter, so an offset would skip rows.
            stmt = (select(table).where(stale, table.id > last_id)
                    .order_by(table.id).limit(int(batch)))
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return updated
            vectors = await _embed_many(embedder, [text_of(r) for r in rows])
            for row, vec in zip(rows, vectors):
                row.embedding, row.embedding_model = vec, model
            await session.flush()
            updated += len(rows)
            last_id = rows[-1].id

    memories = await fill(Memory, lambda m: m.content)
    tags = await fill(Tag, _tag_text)
    return {"updated": memories, "tags": tags}


# ---- tag review: the model's verdict on a tag -------------------------------
async def unverified_tag_ids(session, *, limit=None) -> list[int]:
    """The ids of the unverified tags, oldest first (tags have no date, so
    by id): the order the catch-up reviews them in, after the memories.
    `limit` of 0 or None means all."""
    stmt = select(Tag.id).where(Tag.review_status == UNVERIFIED).order_by(Tag.id.asc())
    if limit:
        stmt = stmt.limit(int(limit))
    return list((await session.execute(stmt)).scalars().all())


async def _live_tag_counts(session, tag_ids=None) -> dict[int, int]:
    """How many live memories use each tag, as `{tag id: count}`; a tag no
    live memory uses is missing."""
    stmt = (select(MemoryTag.tag_id, func.count())
            .join(Memory, Memory.id == MemoryTag.memory_id)
            .where(_archived_cond())
            .group_by(MemoryTag.tag_id))
    if tag_ids is not None:
        stmt = stmt.where(MemoryTag.tag_id.in_(list(tag_ids)))
    return dict((await session.execute(stmt)).all())


async def tag_review_input(session, tag_id: int) -> tuple[dict, list[dict], list[dict]] | None:
    """What the tag reviewer gets for tag `tag_id`, as `(tag, memories,
    neighbours)`, or None when there is no such tag.

    `tag` has its id, name, description and `count`, the live memories that
    use it. `memories` are `tag_review.MEMORY_COUNT` of them, newest first
    (project, type, content). `neighbours` are up to
    `tag_review.NEIGHBOUR_COUNT` verified tags, the tag itself left out:
    with a vector, the closest to the tag's own by cosine (only vectors from
    the same model), best first; without one, the most used. As with the
    memories, only verified tags serve as reference."""
    tag = await session.get(Tag, tag_id)
    if tag is None:
        return None
    counts = await _live_tag_counts(session)
    rows = (await session.execute(
        select(Memory.project, Memory.type, Memory.content)
        .join(MemoryTag, MemoryTag.memory_id == Memory.id)
        .where(MemoryTag.tag_id == tag_id, _archived_cond())
        .order_by(Memory.timestamp.desc(), Memory.id.desc())
        .limit(tr.MEMORY_COUNT))).all()
    memories = [{"project": p, "type": t, "content": c} for p, t, c in rows]
    others = (await session.execute(
        select(Tag).where(Tag.review_status == VERIFIED, Tag.id != tag_id))).scalars().all()
    if tag.embedding is not None:
        mine = list(tag.embedding)
        scored = [(cosine(mine, list(o.embedding)), o) for o in others
                  if o.embedding is not None and o.embedding_model == tag.embedding_model]
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        picked = [(o, score) for score, o in scored]
    else:
        ranked = sorted(others, key=lambda o: (-counts.get(o.id, 0), o.name))
        picked = [(o, None) for o in ranked]
    neighbours = [{"name": o.name, "description": o.description,
                   "count": counts.get(o.id, 0), "score": score}
                  for o, score in picked[:tr.NEIGHBOUR_COUNT]]
    me = {"id": tag.id, "name": tag.name, "description": tag.description,
          "count": counts.get(tag.id, 0)}
    return me, memories, neighbours


def _names_match(a: str, b: str) -> bool:
    """Whether two tag names are one after the name cleanup: the same clean
    name, or one is the plural or singular of the other."""
    ca, cb = clean_tag_name(a), clean_tag_name(b)
    return ca == cb or cb in _plural_forms(ca) or ca in _plural_forms(cb)


def same_tag(tag: Tag, other: Tag) -> bool:
    """The second check a merge needs before the server applies it on its
    own: the names match after the cleanup, or both tags have a vector from
    the same model and those score `tag_review.MERGE_THRESHOLD` or more."""
    if _names_match(tag.name, other.name):
        return True
    if (tag.embedding is None or other.embedding is None
            or tag.embedding_model != other.embedding_model):
        return False
    return cosine(list(tag.embedding), list(other.embedding)) >= tr.MERGE_THRESHOLD


def _dump_proposal(r: TagReview) -> dict:
    return {"id": r.id, "tag": r.tag_name, "tag_id": r.tag_id, "verdict": r.verdict,
            "into": r.into, "new_name": r.new_name, "reason": r.reason, "model": r.model,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
            "resolved": r.resolved}


async def set_tag_review(session, tag_id: int, verdict, model: str,
                         embedder: Embedder | None = None) -> dict:
    """Act on the model's `verdict` (a `tag_review.TagVerdict`) for tag
    `tag_id` and say what was done, as `{"tag", "verdict", "status",
    "merged_into", "proposal"}`.

    keep: the tag is verified; nothing else is stored. merge: when the tag
    to merge into exists and `same_tag` agrees, the merge is applied at once
    with `merge_tags` (the tag goes, its memories move to the other one)
    and a row is stored as `applied`, so the change leaves a trace. Every
    other merge, and every rename or drop, flags the tag and stores the
    proposal for a person (`proposal` is its id). Raises `LookupError` when
    there is no such tag."""
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise LookupError(f"Tag #{tag_id} not found")
    out = {"tag": tag.name, "verdict": verdict.verdict, "status": None,
           "merged_into": None, "proposal": None}
    if verdict.verdict == tr.KEEP:
        tag.review_status = VERIFIED
        out["status"] = VERIFIED
        await session.flush()
        return out
    target = await _find_tag(session, verdict.into) if verdict.verdict == tr.MERGE else None
    auto = target is not None and target.id != tag.id and same_tag(tag, target)
    row = TagReview(tag_id=tag.id, tag_name=tag.name, verdict=verdict.verdict,
                    into=verdict.into, new_name=verdict.new_name, reason=verdict.reason,
                    model=model, created_at=datetime.now(timezone.utc),
                    resolved=tr.APPLIED if auto else None)
    session.add(row)
    await session.flush()
    out["proposal"] = row.id
    if auto:
        await merge_tags(session, [tag.name], target.name, embedder=embedder)
        row.tag_id = None  # the tag is gone; the database cleared it too
        out["merged_into"] = target.name
    else:
        tag.review_status = FLAGGED
        out["status"] = FLAGGED
        await session.flush()
    return out


async def tag_proposals(session, *, pending=True) -> list[dict]:
    """The stored tag verdicts, oldest first. With `pending` (the default)
    only those that wait for a person: not resolved, and the tag still
    there."""
    stmt = select(TagReview).order_by(TagReview.created_at.asc(), TagReview.id.asc())
    if pending:
        stmt = stmt.where(TagReview.resolved.is_(None), TagReview.tag_id.is_not(None))
    return [_dump_proposal(r) for r in (await session.execute(stmt)).scalars().all()]


async def _open_proposal(session, pid: int) -> tuple[TagReview, Tag | None] | None:
    """The proposal `pid` and its tag, or None when there is no such
    proposal. Raises `ValueError` when it is already resolved."""
    row = await session.get(TagReview, pid)
    if row is None:
        return None
    if row.resolved is not None:
        raise ValueError(f"Proposal #{pid} is already {row.resolved}")
    tag = await session.get(Tag, row.tag_id) if row.tag_id is not None else None
    return row, tag


async def apply_tag_proposal(session, pid: int, embedder: Embedder | None = None) -> dict | None:
    """Do what proposal `pid` says: merge the tag into `into` (`merge_tags`),
    rename it to `new_name` (`patch_tag`; a name that is taken merges into
    that tag, and the tag that is left is verified), or delete it
    (`delete_tag`). Mark the proposal `applied` and return
    `{"proposal": <the row>, "result": <what the tag route would return>}`.
    None when there is no such proposal; `ValueError` when it is already
    resolved, its tag is gone, or the tag to merge into is gone. No undo."""
    found = await _open_proposal(session, pid)
    if found is None:
        return None
    row, tag = found
    if tag is None:
        raise ValueError(f"Proposal #{pid}: the tag '{row.tag_name}' no longer exists")
    if row.verdict == tr.MERGE:
        target = await _find_tag(session, row.into or "")
        if target is None:
            raise ValueError(f"Proposal #{pid}: the tag '{row.into}' to merge into no longer exists")
        result = await merge_tags(session, [tag.name], target.name, embedder=embedder)
        row.tag_id = None
    elif row.verdict == tr.RENAME:
        new_name = clean_tag_name(row.new_name or "") or (row.new_name or "").strip()
        result = await patch_tag(session, tag.name, new_name=new_name, embedder=embedder)
        kept = await _find_tag(session, result["name"])
        kept.review_status = VERIFIED
        if kept.id != row.tag_id:  # the name was taken: merged into that tag
            row.tag_id = None
    else:
        result = await delete_tag(session, tag.name)
        row.tag_id = None
    row.resolved = tr.APPLIED
    await session.flush()
    return {"proposal": _dump_proposal(row), "result": result}


async def reject_tag_proposal(session, pid: int) -> dict | None:
    """Turn proposal `pid` down: mark it `rejected` and verify its tag, which
    a person has now chosen to keep as it is. Returns the row; None when
    there is no such proposal; `ValueError` when it is already resolved."""
    found = await _open_proposal(session, pid)
    if found is None:
        return None
    row, tag = found
    row.resolved = tr.REJECTED
    if tag is not None:
        tag.review_status = VERIFIED
    await session.flush()
    return {"proposal": _dump_proposal(row)}
