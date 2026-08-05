# Where to run this

Short answer: **the Raspberry Pi, running the built-in daemon under systemd.**
It costs about $9/year in electricity, you already own it, and it's the only
option here where a failure is diagnosed by walking over to it.

The rest of this page is why, and how.

---

## Why not cron

This matters more than the choice of hardware, so it comes first.

Cron fires a job at a wall-clock time and forgets it. If the machine is asleep,
unplugged, or mid-reboot at 9:00am, **that run never happens** — cron doesn't
catch up. On a machine that isn't reliably on 24/7, that's a silent gap in a
pipeline whose whole job is to notice postings early.

The built-in daemon (`jobhunt daemon`) records which slots have run today in
the database and checks every 5 minutes. Boot it late and it recovers:

| Situation | cron | `jobhunt daemon` |
|---|---|---|
| Pi off overnight, boots 11:00am | 9am run lost | 9am run fires at 11:00 |
| Pi off all day, boots 10pm Friday | both runs + digest lost | one catch-up cycle, digest sent |
| Normal 24/7 uptime | fires 9:00 / 17:30 / 21:00 | identical |
| Restart at 9:01, just after a run | — | does *not* re-run; won't double-send |

So: use `daemon` on any machine that might not be on, which is every machine
you actually own. `deploy/crontab.example` is still there if you're on a server
with real uptime and prefer cron.

---

## Option 1 — Raspberry Pi (recommended)

Any Pi from a 3B onward is far more machine than this needs. The workload is a
few hundred HTTPS requests twice a day and a SQLite file.

**Cost:** ~4W continuous ≈ 35 kWh/year ≈ **$9/year** at NYC rates. Nothing else.

**Why it fits this app specifically:** everything is outbound. It scrapes over
HTTPS and sends mail via Gmail's SMTP. Nothing needs to reach *in*, so there's
no port forwarding, no dynamic DNS, no public attack surface. The web console
binds to your LAN and you open it from your laptop.

```bash
# Pi OS Lite (64-bit). Set the timezone first or 5:30pm lands somewhere odd.
sudo timedatectl set-timezone America/New_York

sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone git@github.com:Spicerke/JobFinder.git ~/jobhunt && cd ~/jobhunt
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m jobhunt init --resume ~/resume.tex
.venv/bin/python -m jobhunt check-boards     # fix any wrong slugs first
```

Secrets go in `~/jobhunt/.env` (never in the database, never in git):

```bash
JOBHUNT_SMTP_PASSWORD=your-gmail-app-password
JOBHUNT_WEB_PASSWORD=something-long
JOBHUNT_SECRET_KEY=$(openssl rand -hex 32)
```

Then install the service so it starts at boot and restarts on crash:

```bash
sudo cp deploy/jobhunt.service /etc/systemd/system/
sudo nano /etc/systemd/system/jobhunt.service   # check User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now jobhunt
journalctl -u jobhunt -f                        # watch the first cycle
```

Console at `http://<pi-ip>:8000` from any machine on your network.

**The one real risk is SD card corruption** — cheap cards die from constant
small writes, and SQLite in WAL mode writes often. Two mitigations, do both:

1. Boot from a USB SSD instead of an SD card if you can. A $15 drive removes
   the problem entirely.
2. Back the database up somewhere off the Pi:
   ```cron
   0 3 * * *  sqlite3 ~/jobhunt/jobs.db ".backup ~/backup/jobs-$(date +\%u).db"
   ```
   That keeps a rotating week. Copy it to your laptop or a cloud drive
   periodically — losing it means losing your application history, which is
   the part you can't re-scrape.

---

## Option 2 — a $3.50–5/month VPS

Worth it if the Pi is a hassle, you move around, or your home internet is
unreliable. Lightsail's cheapest Linux instance is **$3.50/month** IPv6-only
(512 MB / 20 GB), or **$5/month** with a public IPv4 address. Any comparable
VPS works identically — nothing here is AWS-specific.

Setup is the same as the Pi, or use Docker:

```bash
cp .env.example .env      # the three secrets
docker compose up -d
```

Don't expose port 8000. Use an SSH tunnel and delete the `ports:` block:

```bash
ssh -L 8000:localhost:8000 user@your-instance   # then open localhost:8000
```

See `AWS.md` for the AWS-flavoured details, including Fargate and why Lambda is
the wrong shape for this.

**Oracle Cloud's Always Free tier** is a real $0 option and people run things
like this on it for years. Two caveats: the free ARM allocation was cut to
2 OCPU / 12 GB in June 2026, and free ARM capacity is genuinely hard to get in
popular regions — you may sit in a "no capacity" loop for a while. The AMD
micro instance is easier to obtain and still plenty for this.

---

## What not to bother with

- **Lambda / Cloud Functions.** Stateful SQLite, a multi-minute scrape, and an
  always-on web console — three things serverless is bad at. Details in
  `AWS.md`.
- **Fargate.** ~$10–15/month for the same result as a $5 VPS, plus EFS so the
  database survives restarts.
- **Leaving your laptop on.** It sleeps, and a sleeping laptop misses runs. If
  you do this anyway, use `daemon` rather than cron so it catches up on wake.

---

## Whichever you pick

- **Set the timezone.** The 9am/9pm scrapes and the Friday 5:30pm digest are
  local to the host. Containers default to UTC — `docker-compose.yml` sets
  `TZ: America/New_York`.
- **The `.env` is the only place secrets live.** `JOBHUNT_SMTP_PASSWORD` must
  be a Gmail *App Password*, not your account password.
- **Back up `jobs.db`.** Postings can be re-scraped; your application history
  and notes cannot.
- **Confirm it's actually running** after a day or two: the Activity page shows
  per-board pull history, so a silently broken slug or a dead scheduler is
  visible rather than looking like "no new jobs this week".
