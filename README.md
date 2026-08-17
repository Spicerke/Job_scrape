# jobhunt

Pulls company job boards twice a day, scores every posting against each
tailored version of your resume, filters out the ones you can't apply for, and
emails you a ranked digest Friday at 5:30pm — plus same-day alerts for
companies you're watching.

No LLM API, no API key, no per-run cost. Scoring is TF-IDF and weighted keyword
matching implemented directly in the standard library, so the whole thing runs
from cron on a laptop or a $5 VPS.

Settings live in the database and are edited from the web console, so the
scheduled runs and the UI can never disagree about what you're looking for.

```
  ATS APIs ──► scrape ──► SQLite ──► score ──► screen ──┬─► Friday 5:30pm digest
  (5 vendors)   9am+9pm     ▲       (0-100,    (eligible └─► instant alerts
                            │       per resume  or not)
                            │        variant)
                       web console ────────────────────► settings, resume,
                                                          companies, tracking
```

| When | What happens |
|---|---|
| 9:00am and 9:00pm daily | Scrape all boards, re-score, fire alerts for target companies and target roles |
| Friday 5:30pm | Weekly digest of everything that cleared the filters |
| The moment a target company posts | Same-day email — at the next 9am/9pm pull, not a week later |

## Setup

```bash
pip install -r requirements.txt
python -m jobhunt init --resume resumes/main.tex   # one variant per version
python -m jobhunt resume add resumes/*.pdf         # the files you actually send
python -m jobhunt check-boards     # verify the seeded company slugs
python -m jobhunt scrape           # first pull
python -m jobhunt web              # console at http://127.0.0.1:8000
```

`check-boards` matters. The 28 companies in `companies.yaml` are a starting
point written from memory — some slugs will be wrong on day one. The command
tells you exactly which, and you fix them on the Companies page. A slug is the
last path segment of a company's job board URL:

```
job-boards.greenhouse.io/SLUG   jobs.lever.co/SLUG   jobs.ashbyhq.com/SLUG
apply.workable.com/SLUG         jobs.smartrecruiters.com/SLUG
```

## The web console

- **Jobs** — everything ranked, with the score meter, matched keywords, and the
  reason anything was filtered. Search, filter by score, flip to "Filtered out"
  to check the screener isn't eating things it shouldn't.
- **Applications** — everything you've applied for and what happened next.
  See below.
- **Settings** — keywords and weights, locations, eligibility filters, roles
  that trigger instant alerts, email delivery, schedule. Save and re-rank
  applies changes to postings already in the database.
- **Companies** — add or remove boards, and toggle the watchlist flag that
  means "email me the moment they post".
- **Activity** — per-board pull history, so a silently broken slug is visible.

Resume upload is on the Settings page. It accepts `.tex`, `.txt`, `.md`,
`.pdf`, takes several files at once, and re-ranks automatically.

## Several resumes

Keep a tailored version per kind of role and the app scores each posting
against all of them, then tells you which to send. Three project layouts are
recognised, and you don't have to restructure anything to use one.

**One file per version** — the Overleaf layout, and what this repo uses.
`main.tex` holds the preamble, contact block, and education, then `\input`s a
single variant with the rest commented out:

```latex
%\input{variant-ml}
%\input{variant-research}
\input{variant-swe}
```

All three become variants. **Commented-out inputs count** — they're the
versions you aren't compiling right now, not versions you deleted. Point the
app at `main.tex` alone and it follows the includes:

```bash
python -m jobhunt resume add resumes/main.tex
```

**Markers in a single file** — `%%% VARIANT: Name`, a LaTeX comment, so
`pdflatex` ignores it and the file compiles unchanged. Everything above the
first marker is shared. `\begin{variant}{Name} … \end{variant}` works too.

**Separate documents** — three PDFs, three `.tex` files, whatever. Each is one
variant, named from its filename with the parts common to all of them removed,
so `Spicer-SWE-Resume.pdf`, `Spicer-ML-Resume.pdf`, and
`Spicer-Research-Resume.pdf` become `SWE`, `ML`, and `Research`.

A resume with none of this is a single variant, exactly as before.

### The PDF you actually send

Variants carry two things: the **text** they're matched on and the **file** you
attach to an application. Add a PDF whose name matches an existing variant and
it attaches as that variant's file while the LaTeX text is kept for matching —
`.tex` is the cleaner source of tokens, the PDF is the thing you email:

```bash
$ python -m jobhunt resume add resumes/Spicer-SWE-Resume.pdf
variant 'SWE': attached Spicer-SWE-Resume.pdf, kept variant-swe.tex text
```

Re-export from Overleaf and re-run that line; nothing else changes. The files
live in the database, so `jobhunt backup` captures them and the Settings page
hands you the right one on a click.

```
$ python -m jobhunt resume list
  1  ML               443w  variant-ml.tex         Spicer-ML-Resume.pdf
     ML ENGINEER -- your primary version, send this most often
  2  Research         440w  variant-research.tex   Spicer-Research-Resume.pdf
  3  SWE              409w  variant-swe.tex        Spicer-SWE-Resume.pdf
```

Every posting is then scored against every variant and told which one to send:

```
79.8  Machine Learning Intern   DreamCo
      send the 'ML' resume — ML 80, Research 61, SWE 58
```

The winner is whichever variant scores highest. Only the TF-IDF arm moves
between them — keyword and title matching are identical — so the spread tells
you how clear-cut the call was. A 20-point gap means send that one; a 2-point
gap means it barely matters. The digest, the job list, and `jobhunt show <id>`
all report it.

Screening is deliberately *not* per-variant: "can I apply at all" reads the
whole document, so a requirement covered by any one variant counts as covered.

## Tracking what you applied for

Hit **Applied** on any posting and it moves into the Applications dashboard.
From there each one walks a pipeline:

```
interested → applied → heard back → interview → offer
                                              ↘ rejected / withdrawn
```

The dashboard shows a funnel across the top — applied, still live, heard back,
offers, rejected, and your **response rate** — then the applications themselves,
colour-coded by stage down the left edge. Each row edits in place: change the
stage, set a next step with a due date, add notes.

**Needs a nudge** is the part that earns its keep. It surfaces anything with a
follow-up date that's passed, or sitting at "applied" for 14+ days with no
reply — the applications that quietly go stale otherwise.

Every stage change is written to a history table, so each application has a
dated timeline of what happened and when, and the response rate is computed
from real transitions rather than a number you maintain by hand. The posting's
best-matching resume variant is shown alongside, so you can see which one you
sent six weeks later.

From the terminal:

```bash
jobhunt apps                          # the live pipeline + funnel
jobhunt apps --show closed
jobhunt track 218 applied --notes "referred by Dana"
jobhunt track 218 interview --next "send thank-you note" --by 2026-08-06
```

## How a job gets scored, and how it gets rejected

**Score (0–100)** is a weighted blend, all three weights editable:

| Signal | Default | What it measures |
|---|---|---|
| keyword | 0.40 | Weighted skill terms found in the posting |
| resume | 0.35 | TF-IDF cosine between your resume and the posting — the best-scoring variant wins |
| title | 0.25 | How closely the title matches your role patterns |

The TF-IDF index is built from every posting in your own database, so "Python"
scores low and "graph neural network" scores high. It's pure Python — no numpy
or scikit-learn — so it runs from cron without a heavy environment.

**Screening** is a separate pass that answers "can I apply for this at all".
Three of its checks read your resume:

- **Degree level** — drops a role that requires a PhD when you hold a BS. A
  posting saying "BS/MS" is fine; only a hard floor above you is rejected.
- **Graduation class** — drops "graduating in 2026" roles when you're 2027.
  Off by default, since some postings phrase this loosely.
- **Requirement coverage** — finds the technologies named under the posting's
  Requirements heading and checks what share appear on your resume. Below the
  threshold, it's rejected.

That last one is the useful one. A posting titled "Software Engineer Intern"
scores a perfect 1.0 on title match, and if its requirements are Scala, Kafka,
Cassandra, Terraform, and Azure, you still can't apply. Coverage catches it:

```
33.0  Software Engineer Intern, Platform   Meridian Health
      [filtered: resume covers 0% of requirements
       (missing ansible, azure, cassandra, elasticsearch)]
```

Nothing is deleted. Filtered jobs stay in the database with their reason
attached, visible under Show → "Filtered out". If a filter is too aggressive
you'll see it there rather than wondering what you missed.

## Running it automatically

**Use the built-in scheduler, not cron** — unless the host has real 24/7
uptime:

```bash
python -m jobhunt daemon --with-web
```

One process, scheduler plus web UI. It records which slots have run today in
the database and checks every 5 minutes, so a machine that was asleep at 9am
**catches up when it wakes** instead of silently losing the run. A restart
never double-sends. Cron has no such recovery — a missed 9am is gone.

`deploy/crontab.example` is there if you're on a server that genuinely never
goes down:

```cron
0 9,21 * * *  cd ~/jobhunt && python3 -m jobhunt run     # 9am + 9pm
30 17 * * 5   cd ~/jobhunt && python3 -m jobhunt digest  # Friday 5:30pm
```

Both are local to the machine — set the timezone, or check `TZ` in a container.

**Where to run it: see [`deploy/HOSTING.md`](deploy/HOSTING.md).** Short answer
for a personal job search — a Raspberry Pi under your desk, ~$9/year in
electricity, systemd unit in `deploy/jobhunt.service`. A $3.50–5/month VPS if
you'd rather not own the hardware.

**Docker:**

```bash
cp .env.example .env      # three secrets
docker compose up -d
```

**AWS:** see `deploy/AWS.md`. Short version: a $5 Lightsail instance with
docker compose and an SSH tunnel beats Fargate for this, and Lambda is the
wrong shape for it.

**systemd:** `deploy/jobhunt.service`.

## Security

Three environment variables, none of them stored in the database:

| Variable | Purpose |
|---|---|
| `JOBHUNT_SMTP_PASSWORD` | Gmail App Password (not your account password) |
| `JOBHUNT_WEB_PASSWORD` | Login for the console |
| `JOBHUNT_SECRET_KEY` | Signs session cookies; keeps you logged in across restarts |

Put them in `.env` at the project root. systemd and docker-compose inject that
file themselves; the CLI also reads it directly, so `python -m jobhunt digest`
run by hand over SSH has the SMTP password too. Anything already exported in
the environment wins.

The app **refuses to bind to a non-localhost interface without
`JOBHUNT_WEB_PASSWORD` set**. The settings page holds your email address and
SMTP username; it shouldn't be open to the internet.

`.env` and your resumes are both gitignored. If you host this repo publicly,
keep it that way — a resume carries your phone number and personal email. Copy
both to the Pi with `scp`; see `deploy/HOSTING.md`.

Once you have a Gmail App Password, confirm the machine can actually send:

```bash
python -m jobhunt email test
```

## Commands

| Command | What it does |
|---|---|
| `init` | Create the database, seed settings, boards, and resumes |
| `resume add / list / remove / attach` | Manage resume variants and the PDF each one sends |
| `check-boards` | Verify every company slug resolves |
| `scrape` | Pull all boards and re-rank |
| `run` | scrape + score + alerts — what cron calls each morning |
| `daemon` | Built-in scheduler. `--with-web` to serve the UI too |
| `web` | Serve the console |
| `score` | Re-rank against current settings |
| `digest` / `alerts` | Send email. `--dry-run` writes an HTML preview |
| `list` / `show <id>` | Terminal views, with score breakdown |
| `track <id> <status>` | Move an application along. `--notes`, `--next`, `--by` |
| `apps` | The application pipeline and your response rate |
| `backup` | Consistent snapshot, safe while the daemon runs. `--to`, `--keep` |
| `stats` | Counts and the top reasons jobs are being filtered |
| `config show / export / import` | Move settings between YAML and the database |
| `email test` | Send yourself one message to prove SMTP works from this box |

## Tuning

Run `stats` after a week — it prints the top filter reasons. If one reason is
discarding hundreds of postings, that filter is too aggressive.

The usual culprit is **locations**. It matches the posting's location field
only, so `united states` will *not* catch `Mountain View, CA`. Use cities and
state codes, or empty the list to accept anywhere.

Then open a few postings you like and check the score breakdown. The missing
keywords list is where you and the scorer disagree; adjust weights, re-rank,
compare.

`min_score: 45` is a guess until you've seen real data. Set the filter to 0 and
find where results stop being interesting.

## Notes

- Requests are throttled to ~1.6/sec per host with retry and backoff. These are
  free public endpoints; hammering them is how they stop being public.
- SmartRecruiters needs a second request per posting for the description, so
  those boards are slower.
- Jobs unseen for 10 days are marked closed, not deleted.
- `config.yaml` is only a seed for `init`. After that the database is the
  source of truth — use `config export` to write it back out.
