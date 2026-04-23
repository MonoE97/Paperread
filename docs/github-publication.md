# GitHub Publication Checklist

## Local Repository

- Repository path: `/Users/jwxi/Desktop/AIflow/Zotero_paperread`
- Default branch: `main`
- Suggested GitHub repository name: `zotero-paperread`
- Package name: `zotero-paperread`

## Before Creating Remote

Run:

```bash
git status --short --branch
uv run pytest
uv run zotero-paperread --help
rg -n "token|password|secret|api[_-]?key|Bearer" .
```

Expected:

- working tree contains only intended changes;
- tests pass;
- CLI help renders;
- secret scan has no real secrets.

## Approval Gate

Stop before any of these actions unless the user explicitly approves:

- `gh repo create`
- `git remote add origin ...`
- `git push`
- publishing releases or packages

## Suggested First Remote Setup After Approval

```bash
gh repo create zotero-paperread --private --source=. --remote=origin
git push -u origin main
```

Use `--public` only if the user explicitly chooses public visibility.
