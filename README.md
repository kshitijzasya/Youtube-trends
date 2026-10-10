# yt-trends

A daily job that finds the top trending topics in your YouTube niche and posts them to Slack.
It runs in Docker on GitHub Actions and costs nothing.

```
GitHub Actions (daily cron) ─► Docker container (python -m yt_trends)
  1. Collect   tracked channels' uploads + keyword search (last N hours) + YouTube autocomplete
  2. Snapshot  saves today's view counts to Supabase (Postgres), or data/trends.db if no DATABASE_URL
  3. Score     each video vs its own channel's normal + real 24h growth from yesterday's snapshot
  4. Cluster   LLM groups the best videos into candidate topics (Gemini → Groq → keyword fallback)
  5. Rank      performance × channel coverage × novelty (vs last 7 days) × search demand
  6. Enrich    top comments + LLM: why it's trending, viewer questions, video angles, titles
  7. Post      Slack Block Kit message (or --dry-run prints it)
```

## Free services used

| Service | Used for | Free limit | This project uses |
|---|---|---|---|
| YouTube Data API v3 | videos, stats, search, comments | 10,000 units/day | ~300 + 100 per search keyword |
| Gemini API (AI Studio) | topic clustering + details | free tier, rate-limited | 2 requests/day |
| Groq API | backup LLM | free tier, rate-limited | only if Gemini fails |
| Slack incoming webhook | delivery | free plan (counts as 1 of 10 apps) | 1 message/day |
| GitHub Actions | scheduler + runner | public repo unlimited, private 2,000 min/month | ~3 min/day |
| Supabase | history + records (Postgres) | 500 MB database, 2 projects | a few MB (120-day retention) |

You need only the YouTube key and the Slack webhook. Without any LLM key, topics come from keyword
grouping and the message has no "why trending / angles / titles" details.

---

## Step-by-step setup

### Step 1: Get your keys (about 15 minutes)

**YouTube API key (required)**
1. Go to https://console.cloud.google.com and create a project (no billing needed).
2. **APIs & Services → Library**, search **YouTube Data API v3**, then **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Click the key, then **API restrictions → Restrict key → YouTube Data API v3**, and save.

**Slack webhook (required)**
1. Go to https://api.slack.com/apps, then **Create New App → From scratch**, and pick your workspace.
2. **Incoming Webhooks**: switch it on, then **Add New Webhook to Workspace** and pick the channel.
3. Copy the `https://hooks.slack.com/services/...` URL. Treat it like a password.

**Gemini key (recommended)**: https://aistudio.google.com, then **Get API key**.
Free-tier prompts may be used by Google to improve its products. This project only sends public YouTube data.

**Groq key (optional backup)**: https://console.groq.com, then **API Keys → Create**.

**Supabase database (recommended for keeping records)**
1. Go to https://supabase.com, sign in with GitHub, then **New project**. Choose a region near you (e.g. Mumbai)
   and set a strong database password. Save the password; it's shown only once.
2. Wait about 2 minutes for the project to start. Then click **Connect** at the top of the dashboard.
3. Under **Connection string**, choose **Session pooler** (not "Direct connection").
   - Direct connections use IPv6 only, and GitHub Actions has no IPv6, so they fail with a network error.
   - It looks like: `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`
4. Replace `[YOUR-PASSWORD]` with your password. If the password contains special characters
   (`@ : / ? # %` etc.), URL-encode them (e.g. `@` → `%40`), or pick a password of letters and digits only.
   This full string is your `DATABASE_URL`. Treat it like a password.

The tables are created automatically on the first run, in their own `yt_trends` schema, with
Row Level Security switched on. Supabase's public API can't read them; only this job can.
Without `DATABASE_URL` the project falls back to the local SQLite file, so Supabase is optional.

### Step 2: Configure your niche

Edit `config.yaml`:

```yaml
niches:
  - name: personal-finance
    channels: ["@channel1", "@channel2"]       # handle, URL or UC... id
    search_keywords: ["budgeting", "side hustle"]
    region_code: IN
```

Every option is commented in the file. The ones that change results most:

| Option | Effect |
|---|---|
| `search_keywords` | finds breakout videos from channels you don't track (100 units each) |
| `lookback_hours` | 48 = very fresh, 168 = weekly view |
| `min_duration_sec` | `0` to include Shorts |
| `weights` | e.g. raise `novelty` to stop repeats, raise `coverage` for broad trends |
| `prompts/*.md` | the LLM instructions. Edit freely; `$placeholders` must stay |

To track more niches, add another block under `niches:`. Give it its own `slack_webhook_env` if it should post to a different channel.

### Step 3: Run it locally with Docker

```bash
cd ~/Projects/yt-trends
cp .env.example .env                 # then paste your keys into .env

docker compose --profile test build              # build images (re-run after code changes)
docker compose --profile test run --rm test       # tests on SQLite + a throwaway local Postgres
docker compose run --rm yt-trends --dry-run       # real data, prints the report, no Slack
docker compose run --rm yt-trends                 # posts to Slack
```

The first run says "first run: growth uses lifetime averages". From the second day on,
real 24h growth is measured from the saved snapshots.

The log line `history store: postgres:aws-0-...pooler.supabase.com:5432/postgres schema=yt_trends`
confirms Supabase is in use. `history store: sqlite:data/trends.db` means DATABASE_URL isn't set.

**Already have history in `data/trends.db`?** Copy it into Supabase once. It's safe to re-run; existing rows are kept:

```bash
docker compose run --rm yt-trends --import-sqlite data/trends.db
```

### Step 4: Put it on GitHub

1. Create an empty repo at https://github.com/new, e.g. `yt-trends`.
   - **Public**: unlimited Actions minutes, but the trend history in `data/trends.db` is visible (it's only public YouTube data).
   - **Private**: 2,000 free minutes/month, and this job uses about 90.
2. Push the code:

```bash
cd ~/Projects/yt-trends
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/yt-trends.git
git push -u origin main
```

`.env` is in `.gitignore`, so your keys never leave your machine.

### Step 5: Add secrets on GitHub

Repo **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `YT_API_KEY` | YouTube key |
| `SLACK_WEBHOOK_URL` | Slack webhook |
| `GEMINI_API_KEY` | Gemini key (optional) |
| `GROQ_API_KEY` | Groq key (optional) |
| `DATABASE_URL` | Supabase session pooler string (optional; without it, history is committed to the repo as `data/trends.db`) |

Without `DATABASE_URL`, also set **Settings → Actions → General → Workflow permissions** to **Read and write**.
The workflow needs write access to commit the SQLite history file. With Supabase, nothing is committed.

### Step 6: First run on GitHub

1. Repo **Actions** tab, then **daily-trends**, then **Run workflow**.
2. Tick **dry_run** for the first try. The report appears in the job log under "Run pipeline".
3. Run it again without dry_run. The message lands in Slack.

Every run builds the test image and runs the tests (SQLite plus a throwaway Postgres). It then builds the
runtime image and runs the pipeline. Without Supabase, it also commits `data/trends.db` back to the repo
(`[skip ci]`, so it doesn't trigger itself).

### Step 7: The schedule

`.github/workflows/daily.yml` runs at `30 3 * * *` (03:30 UTC = 09:00 IST). Cron times are in UTC.
Convert at https://crontab.guru. GitHub can start scheduled runs a few minutes late at busy times.

---

## Your records in Supabase

Open **Table Editor**, then pick schema **yt_trends** in the schema dropdown:

| Table | One row per | Useful columns |
|---|---|---|
| `reports` | topic per niche per day | `run_date`, `rank`, `topic`, `score`, `is_new`, `details` (why/angles/titles), `evidence` (videos) |
| `snapshots` | video per run | `views`, `likes`, `comments`, `captured_at`: view history over time |
| `videos` | video seen | `title`, `channel_title`, `published_at` |
| `handles` | tracked handle | cached channel id |

Example queries (**SQL Editor**):

```sql
-- Today's report
select rank, topic, is_new, round(score::numeric, 2) as score, details->>'why_trending' as why
from yt_trends.reports where run_date = current_date order by niche, rank;

-- Topics that keep coming back (last 30 days)
select topic, count(*) as days, min(run_date) as first_seen, max(run_date) as last_seen
from yt_trends.reports where run_date > current_date - 30
group by topic having count(*) > 1 order by days desc;

-- View history of one video
select captured_at, views from yt_trends.snapshots where video_id = 'VIDEO_ID' order by captured_at;
```

Free-plan notes: projects pause after 7 days without activity (the daily run keeps it active).
The free plan has no automatic backups. To keep a copy, export a table to CSV from the Table Editor.

## How a video is scored

```
outlier   = video views / channel's median views (uploads aged 7–90 days)   "3x their usual"
views_24h = views now − views at last snapshot (≥12h old), scaled to 24h
            (first sighting: lifetime average, age clamped to ≥24h)
pace      = views_24h / (channel median / 7)
score     = 0.5·ln(1+outlier) + 0.5·ln(1+pace)
```

Topics are then ranked by the `weights` in `config.yaml`:
- **performance**: mean score of the topic's best 3 videos
- **coverage**: number of distinct channels covering the topic
- **novelty**: 1 − overlap with topics posted in the last `novelty_days`. Topics that overlap get marked 🔁 ONGOING.
- **search demand**: share of autocomplete suggestions that mention the topic

## Project layout

```
config.yaml              behaviour (niches, weights, models)
prompts/cluster.md       LLM prompt: group videos into topics
prompts/details.md       LLM prompt: why trending, questions, angles, titles
src/yt_trends/
  __main__.py            CLI: --dry-run, --niche NAME, --config, --log-level
  pipeline.py            one niche end to end
  youtube.py             YouTube Data API client (quota accounting, no key in errors)
  discovery.py           YouTube autocomplete (unofficial endpoint, fail-soft)
  scoring.py             video + topic scoring (pure functions)
  topics.py              LLM clustering, keyword fallback, enrichment
  llm.py                 Gemini / Groq clients + fallback chain
  store.py               storage interface + SQLite backend
  postgres_store.py      Supabase/Postgres backend (own schema, RLS on)
  slack.py               Block Kit message + text rendering
tests/                   offline tests (fake YouTube/LLM/HTTP)
Dockerfile               targets: test, runtime (default)
.github/workflows/daily.yml
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `configuration error: YT_API_KEY is not set` | Secret missing or misnamed (Step 5), or `.env` missing locally |
| `HTTP 400: API key not valid` | Wrong key, or key restricted to a different API |
| `HTTP 403 ... quotaExceeded` | Daily quota used up. Fewer `search_keywords`, or wait for the reset (midnight Pacific time) |
| `no channel found for '@x'` | Copy the handle exactly from the channel page, or use the `UC...` id |
| `no long-form videos published in the last Nh` | Raise `lookback_hours`, add keywords, or set `min_duration_sec: 0` |
| Message has topics but no angles/titles | No LLM key, or both LLMs failed. See the warnings in the log. Check model names in `config.yaml` against AI Studio / Groq docs |
| `Save history` step fails with 403 | Workflow permissions must be **Read and write** (Step 5) |
| Scheduled runs stopped | GitHub pauses schedules in repos with no activity for 60 days. Re-enable from the Actions tab |
| Slack shows ⚠️ "yt-trends failed" | The error text is in the message. The job also shows red in Actions |
| `history store unavailable: cannot connect ... Network is unreachable` | You used the Direct connection string. Use **Session pooler** (Step 1) |
| `... password authentication failed` | Wrong password, or special characters not URL-encoded in `DATABASE_URL` |
| `... connection timeout expired` | Supabase project paused or still starting. Open the dashboard and click **Restore project** |
| Tables not visible in Supabase | Pick schema `yt_trends` in the Table Editor dropdown (it isn't `public`) |

Exit codes: `0` ok, `1` at least one niche failed (a Slack alert is sent), `2` bad config.
