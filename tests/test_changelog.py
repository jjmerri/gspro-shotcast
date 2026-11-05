from commentator import changelog
from commentator.updater import newer

TEXT = """# What's new
<!-- ## 9.9.9 a note, not a release -->
## 0.3.0
### Stats page
- **Alerts:** on your phone,
  with a second line.
### Booth
- A window.

## 0.2.0
### Booth
- Quieter.

## 0.1.0
### Stats page
- First.
"""


def test_an_update_shows_the_releases_since_the_last_version(tmp_path):
    path = tmp_path / "changelog.md"
    path.write_text(TEXT)
    rels = changelog.parse(TEXT)
    assert [r.version for r in rels] == ["0.3.0", "0.2.0", "0.1.0"]
    assert rels[0].items == {"Stats page": ["**Alerts:** on your phone, with a second line."], "Booth": ["A window."]}
    assert [r.version for r in changelog.between("0.1.0", "0.3.0", path)] == ["0.3.0", "0.2.0"]
    assert changelog.between("0.3.0", "0.3.0", path) == []
    shown = changelog.text(changelog.between("0.2.0", "0.3.0", path))
    assert "Shotcast 0.3.0" in shown and "• Alerts: on your phone" in shown and "**" not in shown


def test_the_shipped_changelog_is_newest_first_with_something_in_each_release():
    rels = changelog.releases()
    assert rels and all(r.items for r in rels)
    assert all(newer(a.version, b.version) for a, b in zip(rels, rels[1:]))
    assert all(set(r.items) <= {changelog.SITE, changelog.BOOTH} for r in rels)
