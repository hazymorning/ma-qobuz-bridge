"""The rules in CLAUDE.md: release notes in a fixed format, and no "we" in the repo's own text."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAX_RELEASE_NOTE_WORDS = 100
HEADING = re.compile(r"### (New|Fixed|Better|Removed)")
BULLET = re.compile(r"- \*\*[^*]+\*\*: \S.*")
FIRST_PERSON_PLURAL = re.compile(
    r"\b(we|we['’](re|ve|ll|d)|us|our|ours|ourselves|let['’]s)\b", re.I
)

# Text this repo writes for people (CLAUDE.md names the banned words, so it is left out)
OWN_TEXT = [
    "README.md",
    "DOCS.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "config.json",
    "docs/music-assistant.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    "translations/en.yaml",
    "qobuz_proxy/webui/static/index.html",
    "qobuz_proxy/webui/static/panel.js",
    "qobuz_proxy/webui/music_assistant_routes.py",
]


def _release_notes(version: str) -> list[str]:
    """The lines of the version's CHANGELOG.md section, as the Release workflow publishes them."""
    lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
    start = lines.index(f"## {version}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [line for line in lines[start:end] if line.strip()]


def test_the_release_notes_list_short_bullets_under_new_fixed_better_or_removed():
    version = json.loads((ROOT / "config.json").read_text())["version"]
    notes = _release_notes(version)

    assert notes, f"CHANGELOG.md has no '## {version}' section"
    assert HEADING.fullmatch(notes[0]), (
        f"Not a '### New|Fixed|Better|Removed' heading: {notes[0]!r}"
    )
    headings = [line for line in notes if HEADING.fullmatch(line)]
    assert len(headings) == len(set(headings)), f"A heading used twice: {headings}"
    for line, following in zip(notes, notes[1:] + [""]):
        if HEADING.fullmatch(line):
            assert BULLET.fullmatch(following), f"No bullet under {line!r}"
        else:
            assert BULLET.fullmatch(line), f"Not '- **what**: explanation' on one line: {line!r}"
    words = sum(len(line.split()) for line in notes)
    assert words <= MAX_RELEASE_NOTE_WORDS, f"{version} release notes: {words} words"


@pytest.mark.parametrize("path", OWN_TEXT)
def test_the_repo_never_says_we(path):
    text = (ROOT / path).read_text()
    found = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), 1)
        if FIRST_PERSON_PLURAL.search(line)
    ]
    assert not found, f"First-person plural in {path}:\n" + "\n".join(found)
