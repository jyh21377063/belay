"""需求抽取：release notes 的机械切分（纯函数）。"""
from __future__ import annotations

from belay.graph.requirements import extract_requirements, quote_in

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
