"""Cross-surface behavior suite.

Every test takes the `driver` fixture, so it runs once per surface (cli, api,
mcp) against ONE live server backed by Postgres. Assertions are on *semantics* —
ids, counts, content, tags — never on surface chrome. Surface-specific output
lives in test_cli_surface.py / test_api_surface.py.
"""


# ---- add ------------------------------------------------------------------
def test_add_returns_incrementing_ids(driver):
    assert driver.add("first") == 1
    assert driver.add("second") == 2


def test_add_stores_content(driver):
    mid = driver.add("hello world")
    assert driver.get(mid).content == "hello world"


def test_add_explicit_agent_is_attributed(driver):
    mid = driver.add("x", agent="clu")
    assert driver.get(mid).agent == "clu"


# ---- query ----------------------------------------------------------------
def test_query_empty(driver):
    assert driver.query() == []


def test_query_returns_full_record(driver):
    driver.add("remember the milk", project="home",
               tags="shopping,food", type="note")
    rows = driver.query()
    assert len(rows) == 1
    m = rows[0]
    assert m.project == "home"
    assert m.type == "note"
    assert m.tags == ["food", "shopping"]   # alphabetical (ORDER BY name)
    assert "remember the milk" in m.content


def test_query_limit(driver):
    for i in range(3):
        driver.add(f"m{i}")
    assert len(driver.query(limit=2)) == 2


def test_query_project_filter(driver):
    driver.add("a", project="alpha")
    driver.add("b", project="beta")
    rows = driver.query(project="alpha")
    assert len(rows) == 1
    assert "a" in rows[0].content


def test_query_agent_filter(driver):
    driver.add("mine", agent="clu")
    driver.add("theirs", agent="tron")
    rows = driver.query(agent="clu")
    assert len(rows) == 1
    assert "mine" in rows[0].content


def test_query_tag_filter(driver):
    driver.add("tagged", tags="auth")
    driver.add("untagged")
    assert len(driver.query(tag="auth")) == 1


def test_query_type_filter(driver):
    driver.add("d", type="decision")
    driver.add("c", type="code")
    assert len(driver.query(type="decision")) == 1


def test_query_since_days_today(driver):
    driver.add("today entry")
    assert len(driver.query(since_days=0)) == 1


def test_query_since_days_excludes_other_days(driver):
    driver.add("today entry")
    assert driver.query(since_days=7) == []


# ---- search ---------------------------------------------------------------
def test_search_match(driver):
    driver.add("the quick brown fox")
    rows = driver.search("brown")
    assert len(rows) == 1
    assert "brown" in rows[0].snippet


def test_search_no_match(driver):
    driver.add("nothing relevant here")
    assert driver.search("zzzznope") == []


def test_search_snippet_highlights_match(driver):
    driver.add("alpha beta gamma delta")
    rows = driver.search("gamma")
    assert len(rows) == 1
    # ts_headline wraps the match with the →/← markers the store configures.
    snip = rows[0].snippet
    assert "gamma" in snip and "→" in snip and "←" in snip


# ---- tags / projects ------------------------------------------------------
def test_tags_empty(driver):
    assert driver.tags() == []


def test_tags_counts(driver):
    driver.add("a", tags="auth")
    driver.add("b", tags="auth,db")
    counts = dict(driver.tags())
    assert counts["auth"] == 2
    assert counts["db"] == 1


def test_projects_empty(driver):
    assert driver.projects() == []


def test_projects_counts(driver):
    driver.add("a", project="alpha")
    driver.add("b", project="alpha")
    assert ("alpha", 2) in driver.projects()


# ---- stats ----------------------------------------------------------------
def test_stats_counts(driver):
    driver.add("a", project="p", tags="t")
    s = driver.stats()
    assert s["total"] == 1
    assert s["tags"] == 1


def test_stats_today_and_agents(driver):
    driver.add("a", agent="clu")
    driver.add("b", agent="tron")
    s = driver.stats()
    assert s["agents"] == 2
    assert s["today"] == 2


# ---- show -----------------------------------------------------------------
def test_show_found(driver):
    mid = driver.add("findable content")
    assert "findable content" in driver.get(mid).content


def test_show_not_found(driver):
    assert driver.get(999) is None


# ---- update ---------------------------------------------------------------
def test_update_content(driver):
    mid = driver.add("old text")
    assert driver.update(mid, content="new text") == "updated"
    assert "new text" in driver.get(mid).content


def test_update_add_and_remove_tags(driver):
    mid = driver.add("x", tags="keep,drop")
    driver.update(mid, add_tags="new", remove_tags="drop")
    tags = driver.get(mid).tags
    assert "keep" in tags
    assert "new" in tags
    assert "drop" not in tags


def test_update_no_changes(driver):
    mid = driver.add("x")
    assert driver.update(mid) == "nochange"


def test_update_not_found(driver):
    assert driver.update(999, content="y") is None


# ---- tag descriptors -------------------------------------------------------
def test_tag_explicit_description(driver):
    mid = driver.add("x", tags=[{"name": "auth", "description": "authentication flow"}])
    assert "auth" in driver.get(mid).tags
    counts = dict(driver.tags())
    assert counts["auth"] == 1
    descs = {n: d for n, _c, d in driver.tags_with_descriptions()}
    assert descs["auth"] == "authentication flow"


def test_tag_new_with_no_description_auto_defaults(driver):
    # A brand-new tag never blocks add()/update() — no description ever required,
    # and its descriptor defaults to its own name.
    mid = driver.add("x", tags=[{"name": "brandnew"}])
    assert "brandnew" in driver.get(mid).tags
    # NB: whether the auto-defaulted descriptor (== the name) is *observable*
    # differs by surface — the CLI hides a redundant '↳ name' line — so that's a
    # surface concern (test_cli_surface / test_repository), not asserted here.


def test_tag_description_can_contain_a_comma(driver):
    # Regression: descriptions are structured JSON fields, not comma-delimited
    # text, so a comma inside one must not split it into extra bogus tags.
    mid = driver.add("x", tags=[{"name": "auth", "description": "logins, oauth, jwt"}])
    assert driver.get(mid).tags == ["auth"]
    descs = {n: d for n, _c, d in driver.tags_with_descriptions()}
    assert descs["auth"] == "logins, oauth, jwt"


def test_tag_description_persists_and_updates(driver):
    driver.add("first", tags=[{"name": "db", "description": "the database"}])
    mid = driver.add("second", tags=[{"name": "db", "description": "storage layer"}])
    assert "db" in driver.get(mid).tags
    counts = dict(driver.tags())
    assert counts["db"] == 2
    # A later explicit description supersedes the earlier one.
    descs = {n: d for n, _c, d in driver.tags_with_descriptions()}
    assert descs["db"] == "storage layer"


# ---- delete ---------------------------------------------------------------
def test_delete_removes(driver):
    mid = driver.add("doomed")
    assert driver.delete(mid) == 1
    assert driver.get(mid) is None


def test_delete_removes_from_search(driver):
    driver.add("keepme aardvark")
    doomed = driver.add("deleteme aardvark")
    driver.delete(doomed)
    assert len(driver.search("aardvark")) == 1


# ---- FTS index consistency ------------------------------------------------
def test_update_reindexes_old_gone_new_found(driver):
    mid = driver.add("findme original orangutan")
    driver.update(mid, content="replaced penguin")
    assert driver.search("orangutan") == []
    assert len(driver.search("penguin")) == 1
