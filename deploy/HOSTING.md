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

### About the SD card

The common warning is "SQLite will wear out your SD card". For this workload
that's not the real risk, and it's worth being precise about why.

**Write volume is a non-issue.** Two scrape cycles a day amount to roughly
100 MB/day, so about **35 GB/year**. Card endurance is measured in terabytes
written, and wear levelling spreads it. You would need decades. Home Assistant
and Pi-hole run SQLite on SD cards for years without wearing them out.

**Unclean power loss is the real risk.** SD cards have no power-loss
protection, and some report a write as durable while it's still buffered. Yank
the plug mid-write and you can corrupt the filesystem or the card's internal
mapping table. So:

- `PRAGMA synchronous` is deliberately left at SQLite's default of FULL. Plenty
  of projects drop it to NORMAL for speed, which risks losing recent commits on
  power loss. Don't change it here — the speed is irrelevant at this scale.
- Shut down with `sudo shutdown -h now` rather than pulling the cord.
- Cut needless writes by making the systemd journal volatile — it's the other
  thing on a Pi that writes constantly, and you don't need it on disk:
  ```bash
  sudo mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' | \
    sudo tee /etc/systemd/journald.conf.d/volatile.conf
  sudo systemctl restart systemd-journald
  ```

**Then take backups, because that's what actually saves you.** A USB SSD is
nice but not required; a card can still fail, and so can the Pi.

```bash
jobhunt backup                          # ~/jobhunt-backups, keeps 7
jobhunt backup --to /mnt/usb --keep 30
```

This uses SQLite's online backup API, so it produces a consistent snapshot
**while the daemon is running** — unlike `cp`, which can catch a half-applied
transaction and give you a file that won't open. Run it nightly:

```cron
0 3 * * *  cd ~/jobhunt && .venv/bin/python -m jobhunt backup >> ~/jobhunt-backup.log 2>&1
```

Copy those off the Pi periodically — a backup on the card you're protecting
against isn't a backup. Postings can be re-scraped; **your application history,
notes, and stage timeline cannot.** That's the part worth protecting.

Restoring is just moving the file back:

```bash
sudo systemctl stop jobhunt
cp ~/jobhunt-backups/jobs-20260805-030000.db ~/jobhunt/jobs.db
sudo systemctl start jobhunt
```

---

## Reading the database from your laptop

The Pi owns `jobs.db` and is the only thing that writes to it. Your laptop
doesn't need a copy or an install — it opens `http://raspberrypi.local:8000`
in a browser and that's the whole integration.

**Don't** put the database on a network share (NFS/SMB) or a sync folder
(Dropbox/iCloud/Syncthing) so both machines can reach it. SQLite's locking is
unreliable over network filesystems and WAL mode explicitly does not work on
them; sync folders give you two writers on two copies and conflicted-copy
files. Either one loses data.

To query it directly, read it over SSH:

```bash
ssh pi@raspberrypi.local 'sqlite3 ~/jobhunt/jobs.db "SELECT title, company, score FROM jobs ORDER BY score DESC LIMIT 10"'
```

or take a snapshot and pull that — safe to poke at locally, including with
`jobhunt web --db snap.db`, as long as you don't expect writes to travel back:

```bash
ssh pi@raspberrypi.local 'cd jobhunt && .venv/bin/python -m jobhunt backup --to /tmp/snap --keep 1'
scp pi@raspberrypi.local:/tmp/snap/jobs-*.db .
```

From outside your home, don't port-forward — use an SSH tunnel
(`ssh -L 8000:localhost:8000 pi@raspberrypi.local`) or put Tailscale on both
machines and reach the Pi by name with nothing exposed.

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
