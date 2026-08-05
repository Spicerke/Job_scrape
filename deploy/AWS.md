# Running jobhunt on AWS

Three options, cheapest first. The thing that shapes all of them is that
jobhunt keeps state in a SQLite file — your postings, your settings, your
last-run dates. Whatever you pick has to give that file a permanent home.

---

## Option 1 — Lightsail or a t4g.micro EC2 box (recommended)

About $5/month, and it's the closest thing to running it on your laptop.
One container, one disk, done.

```bash
# on a fresh Ubuntu 24.04 instance
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

git clone <your repo> jobhunt && cd jobhunt
cp .env.example .env && nano .env      # set the three secrets
docker compose up -d
```

The named volume `jobhunt-data` persists across `docker compose down`, image
rebuilds, and reboots. It does **not** survive deleting the instance, so:

```bash
# back the database up to S3 weekly
0 3 * * 0  docker run --rm -v jobhunt_jobhunt-data:/d -v /tmp:/out alpine \
             cp /d/jobs.db /out/jobs.db && aws s3 cp /tmp/jobs.db s3://YOUR-BUCKET/jobhunt/
```

**Don't expose port 8000 to the internet directly.** Two safe options:

- *SSH tunnel* (simplest — nothing is public at all):
  ```bash
  ssh -L 8000:localhost:8000 ubuntu@your-instance
  # then open http://localhost:8000
  ```
  With this, remove the `ports:` block from docker-compose.yml entirely.

- *Caddy in front*, if you want it reachable from your phone:
  ```
  jobs.yourdomain.com {
      reverse_proxy localhost:8000
  }
  ```
  Caddy gets you HTTPS automatically. `JOBHUNT_WEB_PASSWORD` is then doing
  real work, so make it long.

Set the instance timezone (`sudo timedatectl set-timezone America/New_York`)
or your "Friday 8am" digest arrives at a surprising hour.

---

## Option 2 — ECS Fargate with EFS

Use this if you want no server to patch. Roughly $10–15/month, mostly for the
always-on task and the NAT/EFS overhead — more than option 1 for the same job.

1. Push the image to ECR:
   ```bash
   aws ecr create-repository --repository-name jobhunt
   docker build -t jobhunt .
   docker tag jobhunt:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/jobhunt:latest
   aws ecr get-login-password | docker login --username AWS --password-stdin \
     $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
   docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/jobhunt:latest
   ```

2. Create an EFS filesystem and mount it at `/data` in the task definition.
   **EFS, not ephemeral storage** — Fargate task storage is wiped on every
   restart, which would silently reset your entire database.

3. Put the three secrets in Secrets Manager and reference them from the task
   definition's `secrets` block. Don't put them in `environment`.

4. Run it as a **service with desired count 1**, not a scheduled task. The
   built-in scheduler handles timing, and a long-running service means the
   web UI stays reachable.

Set `TZ` in the task definition — Fargate defaults to UTC.

If you'd rather have EventBridge drive the schedule instead of the built-in
scheduler, run `python -m jobhunt run` as a scheduled task and a separate
service for the web UI. Both mount the same EFS volume. SQLite in WAL mode
handles two processes fine, but keep the schedules from overlapping.

---

## Option 3 — Lambda

Possible, and I'd avoid it. Notes if you try anyway:

- Mount EFS for the database. Lambda's `/tmp` does not persist.
- A full scrape of 30 boards takes several minutes; the 15-minute ceiling is
  close enough to be a real risk as your company list grows.
- The web console needs a separate always-on host or an adapter like Mangum,
  which is more moving parts than option 1 has in total.
- Cold starts make the UI feel broken.

Lambda is the wrong shape for a stateful, long-running, occasionally-slow job.

---

## Which to pick

If you've never run anything on AWS before, use option 1 with an SSH tunnel.
It's the cheapest, has the fewest moving parts, and the failure mode when
something goes wrong is "SSH in and read the log" rather than "read CloudWatch
to find out why the task died."

Honestly — for a personal job search, the free tier of Fly.io or a Raspberry Pi
under your desk both work fine too. Nothing here needs AWS specifically.
