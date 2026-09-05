# FrontDesk for GitHub Issues and Discussions

FrontDesk 1.7 turns a repository's public support traffic into tenant-isolated
conversations. It answers new Issues, Issue comments, Discussions, and Discussion
comments with the `github-support` persona and records the complete thread in the
shared inbox.

## Recommended: GitHub Actions

This route is independent of the operator's desktop OS. GitHub supplies a
short-lived token to the workflow; no personal access token is stored.

Create `.github/workflows/frontdesk-support.yml` in the repository using the
following example.

```yaml
name: FrontDesk support

on:
  issues:
    types: [opened, reopened]
  issue_comment:
    types: [created]
  discussion:
    types: [created, reopened]
  discussion_comment:
    types: [created]

permissions:
  contents: read
  issues: write
  discussions: write

jobs:
  answer:
    runs-on: ubuntu-latest
    steps:
      - uses: ciromu9899/frontdesk@v1
        with:
          provider: openai
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          FRONTDESK_AUTH_MODE: disabled
```

Use an Actions secret for the selected model provider. Never put a model key or
GitHub token in the workflow file, an Issue, a Discussion, or FrontDesk knowledge.
For private/local inference, use a self-hosted runner with Ollama and set
`provider: ollama`.

FrontDesk ignores Pull Requests, edits, closures, unsupported actions, users with
GitHub's Bot type, and logins ending in `[bot]`. This prevents ordinary bot loops.
Each reply contains an invisible delivery marker for traceability.

## Direct webhook deployment

The existing webhook server also exposes `POST /github`. Configure:

```text
FRONTDESK_GITHUB_WEBHOOK_SECRET=<a unique random webhook secret>
FRONTDESK_GITHUB_TOKEN=<a current GitHub App installation or fine-grained token>
FRONTDESK_GITHUB_BOT_LOGIN=<the bot account login>
```

Subscribe only to **Issues**, **Issue comments**, **Discussions**, and
**Discussion comments**. The endpoint verifies `X-Hub-Signature-256` before JSON
parsing and deduplicates `X-GitHub-Delivery` in SQLite. Installation ID is the
tenant boundary; repository ID is the fallback when no installation exists.

The token needs `Issues: write` and `Discussions: write`, plus read access to the
selected repositories. A direct GitHub App installation token is short-lived;
the deployment must refresh it outside FrontDesk. GitHub Actions is the supported
zero-token-management path in this release.

## Shared inbox and escalation

Every accepted post is stored under channel `github`. The conversation key stays
the same for all comments on one Issue or Discussion, so staff see context rather
than disconnected messages. Public GitHub authors remain guests: they can query
approved knowledge but cannot run customer-data, payment, reservation, or admin
tools.

The persona directs security reports to `SECURITY.md` and requests human handoff
for data loss, billing, unsupported claims, or an explicit request for a person.
FrontDesk never claims that a handoff, fix, refund, deadline, or security guarantee
exists unless the corresponding approved workflow produced it.

## Local no-side-effect check

```bash
python -m unittest tests.test_github_support -v
```

These tests simulate signatures, tampering, supported events, bot-loop rejection,
tenant boundaries, durable delivery deduplication, REST Issue replies, GraphQL
Discussion replies, and external API failures. They do not post to GitHub.
