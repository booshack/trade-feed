import sys
import types
from datetime import datetime, timezone

# The production requirements install feedparser. Keeping a tiny import stub
# makes the focused unit tests runnable in a minimal checkout too.
sys.modules.setdefault("feedparser", types.SimpleNamespace(parse=lambda raw: None))

import filter


def report(id_, *, category="representation", title="Signs With Agency (Exclusive)",
           people=None, company="", project="", published=1_000, outlet="Outlet", link=None,
           summary=""):
    return {
        "id": id_, "status": "kept", "category": category, "title": title,
        "people": people or [], "company": company, "project": project,
        "published": published, "first_seen": published, "outlet": outlet,
        "link": link or f"https://{outlet.lower()}.example/{id_}", "summary": summary,
    }


def test_person_normalization_is_full_name_and_removes_suffixes():
    assert filter.person_names(["José García Jr."]) == {"jose garcia"}
    assert not filter.same_news(
        report("a", category="casting", people=["John Smith"], project="Film"),
        report("b", category="casting", people=["Jane Smith"], project="Film"),
    )


def test_representation_requires_same_person_and_agency():
    jackson = report("jackson", people=["Jackson Wang"], company="WME")
    serena = report("serena", people=["Serena Kerrigan"], company="CAA")
    assert not filter.same_news(jackson, serena)
    assert not filter.same_news(
        report("a", people=["Jane Doe"], company="CAA"),
        report("b", people=["Jane Doe"], company="WME"),
    )
    assert filter.same_news(
        report("a", people=["Jane Doe"], company="CAA", outlet="THR"),
        report("b", people=["JANE DOE"], company="CAA", outlet="Variety"),
    )


def test_headline_alone_never_merges():
    a = report("a", category="deals", title="Person Signs With Agency")
    b = report("b", category="deals", title="Person Signs With Agency", outlet="Variety")
    assert not filter.same_news(a, b)
    assert len(filter.build_stories({"a": a, "b": b})) == 2


def test_casting_requires_same_person_and_project():
    same = (
        report("a", category="casting", title="Jane joins Film", people=["Jane Doe"], project="Film"),
        report("b", category="casting", title="Jane cast in Film", people=["Jane Doe"], project="Film", outlet="Variety"),
    )
    assert filter.same_news(*same)
    assert not filter.same_news(
        report("c", category="casting", title="Jane joins Film A", people=["Jane Doe"], project="Film A"),
        report("d", category="casting", title="Jane joins Film B", people=["Jane Doe"], project="Film B"),
    )


def test_build_stories_keeps_every_report_including_same_outlet():
    records = [
        report("a", people=["Jane Doe"], company="CAA", outlet="THR", published=1_000),
        report("b", people=["Jane Doe"], company="CAA", outlet="THR", published=1_100),
        report("c", people=["Jane Doe"], company="CAA", outlet="Variety", published=1_200),
        report("d", people=["Jane Doe"], company="CAA", outlet="Variety", published=1_300),
    ]
    stories = filter.build_stories({r["id"]: r for r in records})
    assert len(stories) == 1
    assert [r["id"] for r in filter.story_reports(stories[0])] == ["a", "b", "c", "d"]
    assert [r["id"] for r in stories[0]["also"]] == ["b", "c", "d"]


def test_duplicate_url_is_one_report_and_time_window_is_respected():
    link = "https://thr.example/same"
    duplicate = [
        report("a", people=["Jane Doe"], company="CAA", link=link, published=1_000),
        report("b", people=["Jane Doe"], company="CAA", link=link, published=1_100),
    ]
    assert len(filter.story_reports(filter.build_stories({r["id"]: r for r in duplicate})[0])) == 1
    far = [
        report("c", people=["Jane Doe"], company="CAA", published=1_000),
        report("d", people=["Jane Doe"], company="CAA", published=1_000 + 97 * 3600),
    ]
    assert len(filter.build_stories({r["id"]: r for r in far})) == 2


def test_archive_uses_same_policy_and_preserves_legacy_alternate(monkeypatch, tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "index.json").write_text('{"months": {}}', encoding="utf-8")
    monkeypatch.setattr(filter, "ARCHIVE_DIR", archive_dir)
    archive = filter.Archive(timezone.utc)
    lead = report("lead", people=["Jane Doe"], company="CAA", published=1_700_000_000, outlet="THR")
    entry = filter.archive_record(lead)
    entry["also"] = [{"outlet": "Legacy", "link": "https://legacy.example/old"}]
    archive.put(entry)
    newer = report("new", people=["Jane Doe"], company="CAA", published=1_700_000_100, outlet="Variety")
    assert archive.add(filter.entry_from_story({"main": newer, "also": [], "reports": [newer]})) is False
    saved = next(iter(archive.load(archive.month_of(1_700_000_000)).values()))
    links = [saved["link"]] + [a["link"] for a in saved["also"]]
    assert "https://legacy.example/old" in links
    assert newer["link"] in links
    assert saved["id"] == lead["id"]


def test_archive_earlier_report_becomes_lead(monkeypatch, tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "index.json").write_text('{"months": {}}', encoding="utf-8")
    monkeypatch.setattr(filter, "ARCHIVE_DIR", archive_dir)
    archive = filter.Archive(timezone.utc)
    earlier_time = int(datetime(2026, 1, 31, 23, tzinfo=timezone.utc).timestamp())
    later_time = earlier_time + 3_600
    later = report("later", people=["Jane Doe"], company="CAA", published=later_time)
    earlier = report("earlier", people=["Jane Doe"], company="CAA", published=earlier_time)
    archive.put(filter.entry_from_story({"main": later, "also": [], "reports": [later]}))
    archive.add(filter.entry_from_story({"main": earlier, "also": [], "reports": [earlier]}))
    entries = archive.load(archive.month_of(later_time))
    assert not entries  # the old lead was removed from its old month
    entries = archive.load(archive.month_of(earlier_time))
    merged = next(iter(entries.values()))
    assert merged["id"] == "earlier"
    assert merged["also"][0]["id"] == "later"


def test_html_and_rss_render_all_alternate_links():
    records = [
        report("a", people=["Jane Doe"], company="CAA", outlet="THR", published=1_000),
        report("b", people=["Jane Doe"], company="CAA", outlet="Variety", published=1_100),
        report("c", people=["Jane Doe"], company="CAA", outlet="Variety", published=1_200),
    ]
    story = filter.build_stories({r["id"]: r for r in records})[0]
    cfg = {
        "categories": {"representation": {"emoji": "✍️", "label": "Representation"}},
        "tags": {}, "feed_items": 10, "title": "Trade Feed", "description": "News",
        "timezone": "UTC", "tz": timezone.utc, "site_url": "", "prices": {"input": 0, "output": 0},
        "model": "test", "feeds": [],
    }
    site = {"base": "", "repo": "", "edit": ""}
    rss = filter.render_feed([story], cfg, site)
    page = filter.render_page([story], {r["id"]: r for r in records}, {"started": 1_000}, cfg,
                              site, {}, 1_300, {})
    for item in records[1:]:
        assert item["link"] in rss
        assert item["link"] in page
    assert "Variety: Signs With Agency (Exclusive)" in rss
    assert "Variety: Signs With Agency (Exclusive)" in page

