# Writing in this repo

Applies to everything written here: code comments, docs, panel text, `CHANGELOG.md`, commit
messages, pull request descriptions and GitHub comments.

- Never write in the first-person plural: no "we", "us", "our", "ours" or "let's". Name who does
  it: the add-on, the backend, Music Assistant, Qobuz, or "you" for the user.
- Plain and direct. Say what happens in as few words as it takes. No filler, no hedging, no
  sales tone.

## Release notes

The `## <version>` section of `CHANGELOG.md` is published as the GitHub release notes.

- At most 100 words.
- One line per bullet, never wrapped: GitHub release notes turn every newline into a line break.
- Every bullet is `- <Label> **<what>**: <short explanation>`, with the label `Fixed`, `Better`,
  `New` or `Removed`.

```markdown
- Fixed **Qobuz in Music Assistant check**: stayed red while Music Assistant's Qobuz provider worked.
- New **Party speakers**: one speaker in the Qobuz app that plays on a whole group.
```

`tests/test_writing.py` checks both rules in CI.
