# pr-review-agent

A self-hosted code review agent for GitHub. It watches pull requests, runs checks against the
lines each PR adds, and leaves **inline review comments** plus a **commit status** that branch
protection can require.

Each review has two passes:

1. **Rule-based checks** always run and are deterministic. They look for committed secrets
   (private keys, AWS/GitHub/Anthropic/Slack tokens, hard-coded passwords), merge conflict
   markers, leftover debugger statements, `console.log`, new TODOs, and very large files.
2. **AI review** (optional) uses Claude (`claude-opus-5-5` by default) to find bugs, security
   problems and logic errors. The model must anchor each finding to a line in the diff. If it
   picks a line GitHub can't comment on, the finding goes into the review summary.

If the AI pass fails (rate limit, outage, refusal), the rule-based results are still posted
and the summary says the AI pass didn't run.

## How it receives pull requests

| Mode | How | Use when |
| --- | --- | --- |
| Webhook | `pr-review-agent serve` exposes `POST /webhook` | You can expose an HTTPS endpoint (recommended) |
| Polling | `pr-review-agent watch owner/repo …` | No inbound network access |
| One-off | `pr-review-agent review owner/repo#123 [--dry-run]` | Local use, CI, debugging |

Webhook and polling can run together: set `PR_REVIEW_WATCH_REPOS` while running `serve`.

The agent reviews a PR when it is opened, reopened, marked ready for review, or gets new
commits. Each head commit is reviewed once, and that record is kept in SQLite so a restart
doesn't post the same review again. A collaborator can comment `/review` on a PR to force a
re-review.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                               # then fill in the values

# Preview findings for a PR without posting anything:
pr-review-agent review your-org/your-repo#42 --dry-run
```

### Docker

```bash
docker build -t pr-review-agent .
docker run -d --name pr-review-agent -p 8080:8080 --env-file .env \
  -v pr-review-data:/data pr-review-agent
```

### Deploy for free with GitHub Actions (no server needed)

This agent is also shipped as a workflow in [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml),
so you don't need a server, domain or hosting at all. It runs the one-off `review`
command on every PR open/update and on `/review` comments.

1. Push the repo to GitHub and enable Actions.
2. (Optional) add an `ANTHROPIC_API_KEY` secret for the AI pass; without it the
   rule-based checks still run.
3. (Optional) for PRs from forks, add a `PR_REVIEW_GITHUB_TOKEN` secret (a fine-grained
   token with Pull requests *Read & write*, Commit statuses *Read & write*, Contents
   *Read-only*). Same-repo PRs work with the built-in `GITHUB_TOKEN`.

The workflow leaves the check green and instead relies on the `pr-review-agent` commit
status (and optional *Request changes* review) as the gate — see below. Because it is
serverless, polling mode (`watch`) and the always-on webhook server are not used.

## GitHub setup

### Option A: GitHub App (recommended for organisations)

Create a GitHub App with these settings:

- **Repository permissions:** Pull requests: *Read & write*, Commit statuses: *Read & write*,
  Contents: *Read-only*, Issues: *Read-only*, Metadata: *Read-only*.
- **Subscribe to events:** *Pull request*, *Issue comment*.
- **Webhook URL:** `https://<your-host>/webhook`. **Webhook secret:** a long random string.

Then set `PR_REVIEW_GITHUB_APP_ID`, `PR_REVIEW_GITHUB_APP_PRIVATE_KEY_PATH` (or
`PR_REVIEW_GITHUB_APP_PRIVATE_KEY`), and `PR_REVIEW_WEBHOOK_SECRET`, and install the app on
your repositories. Comments appear as the app's bot user.

### Option B: Personal access token

Create a fine-grained token with Pull requests *Read & write*, Commit statuses
*Read & write*, and Contents *Read-only* on the target repositories, then set
`PR_REVIEW_GITHUB_TOKEN`. For webhooks, add a repository webhook (content type
`application/json`, events *Pull requests* and *Issue comments*) that points at `/webhook`
and uses the same secret as `PR_REVIEW_WEBHOOK_SECRET`. Comments appear as the token's
owner.

### Making the review a required check

The agent sets a commit status named `pr-review-agent` (configurable). You can mark it as
required in branch protection. It is `failure` when any finding is at or above
`PR_REVIEW_FAIL_ON` (default `error`). With `PR_REVIEW_REQUEST_CHANGES_ON_FAIL=true` the
review is also submitted as *Request changes*.

## Configuration

All settings are environment variables (or `.env` entries). See [.env.example](.env.example).

| Variable | Default | Description |
| --- | --- | --- |
| `PR_REVIEW_GITHUB_TOKEN` | – | Token auth (Option B) |
| `PR_REVIEW_GITHUB_APP_ID` / `_PRIVATE_KEY` / `_PRIVATE_KEY_PATH` | – | GitHub App auth (Option A) |
| `PR_REVIEW_GITHUB_API_URL` | `https://api.github.com` | Set for GitHub Enterprise Server (`https://host/api/v3`) |
| `PR_REVIEW_WEBHOOK_SECRET` | – | Required to accept webhooks; deliveries with a bad signature are rejected |
| `ANTHROPIC_API_KEY` | – | Turns on the AI review pass |
| `PR_REVIEW_LLM_ENABLED` | `true` | Set `false` to run rule-based checks only |
| `PR_REVIEW_LLM_MODEL` | `claude-opus-5-5` | Claude model |
| `PR_REVIEW_LLM_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |
| `PR_REVIEW_MAX_PATCH_CHARS` | `300000` | Diff size budget for the AI pass; files over budget are listed in the summary |
| `PR_REVIEW_MAX_FILES` | `100` | Max changed files reviewed per PR |
| `PR_REVIEW_MAX_INLINE_COMMENTS` | `30` | Further findings go into the summary |
| `PR_REVIEW_FAIL_ON` | `error` | Lowest severity that fails the status: `info` / `warning` / `error` |
| `PR_REVIEW_REQUEST_CHANGES_ON_FAIL` | `false` | Submit *Request changes* instead of *Comment* when the status fails |
| `PR_REVIEW_REVIEW_DRAFTS` | `false` | Review draft PRs |
| `PR_REVIEW_IGNORE_PATHS` | lockfiles, minified, `vendor/*`, `dist/*` | Comma-separated glob patterns |
| `PR_REVIEW_DISABLED_RULES` | – | Comma-separated rule IDs, e.g. `todo-comment,debug-console-log` |
| `PR_REVIEW_WATCH_REPOS` | – | Comma-separated `owner/repo` list to poll |
| `PR_REVIEW_POLL_INTERVAL_SECONDS` | `120` | Polling interval |
| `PR_REVIEW_WORKERS` | `4` | Concurrent reviews |
| `PR_REVIEW_DB_PATH` | `pr_review_agent.db` | SQLite file that records reviewed commits |
| `PR_REVIEW_LOG_LEVEL` / `PR_REVIEW_LOG_JSON` | `INFO` / `false` | Logging |

## Operations

- `GET /healthz` returns status, version and queue depth.
- On shutdown, queued reviews get up to 30 seconds to finish.
- GitHub API calls are retried with backoff on 5xx errors and rate limits.
- Webhook deliveries are acknowledged right away (`202`). Reviews run in a bounded worker
  pool, and repeated events for a PR that is already queued are merged.
- The diff is sent to the Claude API only when `ANTHROPIC_API_KEY` is set. The prompt tells
  the model that PR content is untrusted input.
- Run a **single replica** per database file. The review record is local SQLite.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Code layout (`src/pr_review_agent/`):

- `server.py`: webhook endpoint, signature verification, event routing
- `watcher.py`: polling mode
- `work_queue.py`: bounded queue and worker pool
- `reviewer.py`: one review end to end (fetch, check, post, status)
- `checks.py`: rule-based checks (add new rules to `LINE_RULES` / `FILE_RULES`)
- `llm.py`: Claude review pass with structured JSON output
- `github.py`: GitHub REST client (token or App auth, retries, pagination)
- `diff.py`: unified diff parsing and line mapping

## License

[MIT](LICENSE)
