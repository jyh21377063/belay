"""需求：规则切分、LLM 拆解结果的检查（纯函数）。"""
from __future__ import annotations

import pytest

from belay.graph.requirements import (Draft, check_drafts, drafts_to_requirements, extract_requirements,
                                      parse_drafts, quote_in, source_lines)

MARKDOWN = """The repository at /testbed contains conan at version 2.0.14.
Below are the release notes for version 2.0.15. Implement all changes they describe in the codebase.

<release_notes>
## What's Changed
### Features
* Add `--format` option to `conan list`
* Support the `tools.build:jobs` conf
  for all generators (continuation line)
### Bug fixes
- Fix: crash in `conan cache clean` when the cache is empty
  - nested detail that belongs to the fix
- Bugfix: typo in an error message

## New Contributors
* @someone made their first contribution
</release_notes>
"""

RST = """<release_notes>
Enhancements
^^^^^^^^^^^^
- Add ``split_every`` to ``Series.unique``
- Improve performance of ``merge_asof``

Bug Fixes
^^^^^^^^^
- Fix ``to_parquet`` with empty partitions

Documentation
^^^^^^^^^^^^^
- Update the dashboard docs
</release_notes>"""


def test_markdown_items_sections_and_continuations():
    reqs = extract_requirements(MARKDOWN)
    assert [r.id for r in reqs] == ["R1", "R2", "R3", "R4"]
    assert reqs[0].kind == "new" and reqs[0].section == "Features"
    assert "for all generators" in reqs[1].text                  # 续行并入
    assert reqs[2].kind == "change" and "nested detail" in reqs[2].text   # 嵌套列表项并入
    assert reqs[3].text.startswith("Bugfix: typo")
    assert not any("first contribution" in r.text for r in reqs)  # 贡献者名单不是需求


def test_rst_headings_decide_kinds():
    reqs = extract_requirements(RST)
    assert [(r.kind, r.section) for r in reqs] == [("new", "Enhancements"), ("new", "Enhancements"),
                                                   ("change", "Bug Fixes"), ("docs", "Documentation")]


def test_text_without_items_is_one_requirement():
    reqs = extract_requirements("Make the benchmark at least 2x faster without changing its output.")
    assert len(reqs) == 1 and reqs[0].id == "R1" and "2x faster" in reqs[0].text


def test_quote_check_is_verbatim_modulo_whitespace():
    text = "Fix: crash in `conan cache clean`\nwhen the cache is empty"
    assert quote_in("crash in `conan cache clean` when the cache", text)
    assert quote_in('"Crash in `conan cache clean`"', text)       # 引号与大小写不影响
    assert not quote_in("crash in conan cache clean", text)        # 少了反引号就不是逐字
    assert not quote_in("", text)


def test_rule_split_keeps_the_original_lines_as_quotes():
    reqs = extract_requirements(MARKDOWN)
    assert reqs[1].quotes == ["Support the `tools.build:jobs` conf", "for all generators (continuation line)"]
    assert reqs[1].original() == reqs[1].text


# ---- LLM 拆解结果的检查 ----------------------------------------------------------------

NOTES = """Implement the changes below.
<release_notes>
### Features
* Add `--format` option to `conan list` and a `--dry-run` flag to `conan remove`
* Support the `tools.build:jobs` conf
### New Contributors
* @someone made their first contribution
**Full Changelog**: https://example.com/compare
</release_notes>"""


def test_source_lines_drop_headings_and_contributors():
    assert source_lines(NOTES) == ["Add `--format` option to `conan list` and a `--dry-run` flag to `conan remove`",
                                   "Support the `tools.build:jobs` conf"]


def test_split_bullet_quoted_in_parts_is_covered():
    drafts = [Draft("conan list gets a --format option.", ["Add `--format` option to `conan list`"], "new"),
              Draft("conan remove gets --dry-run.", ["a `--dry-run` flag to `conan remove`"], "new"),
              Draft("The tools.build:jobs conf is supported.", ["Support the `tools.build:jobs` conf"], "new")]
    chk = check_drafts(NOTES, drafts)
    assert chk.ok, chk.feedback()


def test_check_reports_bad_quotes_missing_quotes_kinds_and_gaps():
    drafts = [Draft("conan list gets --format.", ["Add a --format option to conan list"], "feature"),
              Draft("Something.", [], "new")]
    chk = check_drafts(NOTES, drafts)
    assert chk.bad_quotes == [(1, "Add a --format option to conan list")]
    assert chk.no_quote == [2] and chk.bad_kind == [1]
    assert len(chk.uncovered) == 2                      # 引文无效，两行都没被覆盖
    fb = chk.feedback()
    assert any("does not appear verbatim" in f for f in fb) and any("not covered" in f for f in fb)


def test_a_short_fragment_does_not_cover_a_long_line():
    drafts = [Draft("x", ["`conan list`"], "new"), Draft("y", ["Support the `tools.build:jobs` conf"], "new")]
    chk = check_drafts(NOTES, drafts)
    assert chk.uncovered == [source_lines(NOTES)[0]]


def test_drafts_become_requirements_with_only_valid_quotes():
    drafts = [Draft("conan list gets --format.", ["Add `--format` option to `conan list`", "invented text"], "new",
                    "Features"),
              Draft("", ["Support the `tools.build:jobs` conf"], "bogus")]
    reqs = drafts_to_requirements(NOTES, drafts)
    assert [r.id for r in reqs] == ["R1", "R2"]
    assert reqs[0].quotes == ["Add `--format` option to `conan list`"] and reqs[0].section == "Features"
    assert reqs[0].original() == "Add `--format` option to `conan list`"
    assert reqs[1].text == "Support the `tools.build:jobs` conf" and reqs[1].kind == "change"


def test_parse_drafts_accepts_fences_and_a_single_quote():
    text = 'Here:\n```json\n{"requirements": [{"statement": "s", "quote": "q", "kind": "New"}]}\n```'
    d = parse_drafts(text)
    assert d == [Draft("s", ["q"], "new", "")]
    with pytest.raises(ValueError):
        parse_drafts('{"requirements": []}')
    with pytest.raises(ValueError):
        parse_drafts("no json here")
