from phase1.discover import excluded_path, last_page_from_link, sample_windows


def test_excluded_path():
    segs = ["mirrors", "templates", "i18n"]
    assert excluded_path("mirrors/repos/a@b/AGENTS.md", segs)
    assert excluded_path("docs/I18N/tr/CLAUDE.md", segs)
    assert not excluded_path("packages/web/AGENTS.md", segs)
    assert not excluded_path("CLAUDE.md", segs)
    assert not excluded_path("src/templates.md/CLAUDE.md", ["templates"])


def test_last_page_from_link():
    link = ('<https://api.github.com/repositories/1/commits?sha=main&path=CLAUDE.md'
            '&per_page=1&page=2>; rel="next", '
            '<https://api.github.com/repositories/1/commits?sha=main&path=CLAUDE.md'
            '&per_page=1&page=17>; rel="last"')
    assert last_page_from_link(link) == 17


def test_last_page_missing():
    assert last_page_from_link(None) is None
    assert last_page_from_link('<https://x?page=1>; rel="prev"') is None


def test_sample_windows_deterministic_and_prefix_stable():
    a = sample_windows("CLAUDE.md", 10, 100, 100000, 0.0005, 42)
    b = sample_windows("CLAUDE.md", 20, 100, 100000, 0.0005, 42)
    assert a == b[:10]
    assert sample_windows("AGENTS.md", 10, 100, 100000, 0.0005, 42) != a
    for lo, hi in b:
        assert 100 <= lo <= hi <= 100000 * 1.001
