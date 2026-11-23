# Halo 2 Discord Status — Railway Upgrade

Prepared for Evan / Halo Classic Hub, November 23, 2026.

This is a replacement project for the existing Halo2-feed Railway project,
service t800, production environment. It does not need access to the old GitHub
account or the old code. No deployment or Discord message has been made by
preparing these files.

## What the channel shows

Example only — these numbers are illustrative:

**H2 Insignia Status**

Players Online: 45  
Players in Game: 30

**Active Playlists:**

- Big Team Battle: 21
- Solo Arena: 9

**Additional Stats:**

- Best Time to Play Today: 8 PM–9 PM EST (historical estimate)
- Unique Players This Week: 719

All counts are Halo 2-specific, including Players Online. Active Playlists shows
up to five lines: the five playlists with the highest nonzero player counts,
ordered highest first. Ties are alphabetical. If fewer than five playlists are
active, only those playlists appear; unused lines are not filled with zero counts.
The board checks the pages every 60 seconds and edits the same message.
It does not send fresh lobby notifications or role pings. This changes the old
feed's posting behavior to a continuously updated status board. Pin the new
message so members can find it easily.

## Where the numbers come from

| Field | Exact source and meaning |
| --- | --- |
| Players Online | Halo 2 row under Active Games at https://insignia.live/; includes anyone signed into the title, including outside matchmaking. |
| Players in Game | Halo 2's Active Players in that same row; sums occupied public/private match counts, never the available-slot denominator. This follows Insignia's session reporting; it is not inferred from online minus searchers. |
| Active Playlists | Each playlist's displayed player badge at https://insignia.live/halo2. Copies the site's numbers without interpreting them as completed matches or summing them into the in-game figure. |
| Unique Players This Week | Halo 2 row in Popular this week on the homepage; trailing seven days, counted by gamertag, refreshed hourly. Multiple gamertags belonging to one person may count more than once. Not the all-Insignia headline total. |
| Best Time to Play Today | Halo 2's published 7×24 UTC activity grid at /halo2, based on the last 28 days of sign-ins. Finds today's highest hourly score after converting to America/New_York, including the next UTC day and daylight saving time. Ties choose the earliest hour. It considers the whole local day, even if the peak hour has passed. |

The best-time result is our calculation from Insignia's published grid. It is a
historical estimate, not an event schedule or a guarantee of players online.

Missing/unrecognized data shows Unavailable, not a fabricated zero. A completely
empty but valid playlist list shows No active playlists reported right now.
If Halo 2 is absent from the homepage's active-games table, online/in-game show
Unavailable rather than guessing that absence means zero. Source HTML may need
another parser update if Insignia changes its markup again.

## 1. Create your new GitHub account and repository

1. Create/sign into your new account at https://github.com/signup.
2. Create a new repository at https://github.com/new. Suggested name:
   **halo2-insignia-status**. Private is fine.
3. Extract this ZIP on your computer.
4. In the repository, choose **Add file → Upload files** (an empty repository
   also offers an uploading an existing file link).
5. Upload the files INSIDE the extracted folder. `halo2_feed.py`,
   `requirements.txt`, and `Dockerfile` must be at the repository root, not
   inside a second folder. Include README.md and test_feed.py if desired.
6. Commit to the `main` branch.

No webhook URL belongs in GitHub. The ZIP contains no credentials.

## 2. Get the Discord webhook

Open your active-playlists channel → Edit Channel → Integrations → Webhooks.
Copy the webhook for that channel. The existing T-800 webhook can be reused if
you still control it, or create a new webhook in the same channel. Paste its URL
directly into Railway in the next step. You do not need a Discord bot token.

## 3. Configure the existing Railway service

Project link from your screenshot:
https://railway.com/project/c26f271b-f812-4859-9ec2-76272dbad5f0

1. Open **Halo2-feed → production → t800**. Close the deployment details panel
   if necessary to reach the service's Settings and Variables tabs.
2. Keep this service. The current worker can run while you prepare the files.
3. In **Variables**, add:

   | Variable | Value |
   | --- | --- |
   | DISCORD_WEBHOOK_URL | Paste your channel's Discord webhook URL |
   | POLL_SECONDS | 60 |
   | DISPLAY_TIMEZONE | America/New_York |
   | STATE_FILE | /data/state.json |

4. Add a Railway **Volume** attached to t800, with mount path **/data**. The
   volume keeps the Discord message ID across deployments and restarts. If you
   already have a volume, use a suitable path on that volume instead.
5. Under **Settings → Source**, disconnect the old GitHub repository and use
   **Connect Repo** to select the new repository and branch **main**. If the new
   repository is missing, use Railway's GitHub connection/configuration flow
   while signed into the new GitHub account and grant Railway access to this
   repository. Keep access to your current Railway account throughout; do not
   delete it or create a replacement Railway project.
6. The included Dockerfile supplies the build. If Railway asks, select
   Dockerfile as the builder. Keep the start command **python halo2_feed.py**.
   No public domain, HTTP healthcheck, port, or cron schedule is needed.
7. Keep **one replica**, disable Serverless/app sleeping if enabled, and use
   the restart-on-failure policy. Stage the source/config changes together and
   deploy the new source. Ensure the old deployment has stopped after the
   replacement becomes active; do not run a separate old service alongside it.
8. Check **Deploy Logs**. Look for `Starting Halo 2 board`, followed by
   `Created status message. DISCORD_MESSAGE_ID=...` and `Status updated`.
9. Open Discord, confirm the new board, and pin it. After about one minute the
   Checked timestamp should advance on the SAME message.

Railway supports changing a service's GitHub source without recreating the
project. The exact account-connection screen depends on how your current
Railway login is connected to GitHub; if it prevents selecting the new account,
resolve that connection before disconnecting your Railway login method.

### Alternative if you do not want a volume

Omit STATE_FILE (or use state.json), deploy once, then copy the message ID from
Deploy Logs and set **DISCORD_MESSAGE_ID** to that value in Railway. Redeploy.
The webhook must be the same one that created that message. This tells the app
which message to edit even when local state is lost. With neither a persistent
volume nor DISCORD_MESSAGE_ID, a restart/redeploy may create another board.
If the message is deleted and recreated, update DISCORD_MESSAGE_ID again.

## Verification and troubleshooting

- A green Railway deployment only means the process started. Verify both the
  logs and the Discord message.
- A new post every restart means the state volume or message-ID setting is
  missing/misconfigured.
- A 401/403 or Unknown Webhook error means the Discord URL or channel access
  needs checking. An Unknown Message error recreates the board automatically.
- If the source is unavailable, the board displays Unavailable for affected
  fields and retries on the next poll. It never presents an old snapshot as live.
- The weekly source refreshes hourly, even though the board checks every minute.
- This version does not reproduce the old per-lobby announcement system. It
  edits one status board; edits do not generate fresh new-message notifications.
- Both source pages are fetched separately, so rapidly changing counts can be
  a few seconds apart. A Discord outage may leave the last message visible;
  its Checked timestamp tells members when the last successful update occurred.
- There is a small unavoidable creation window: if Discord accepts a new post
  but the connection fails before returning its ID, a later retry can create
  a duplicate. Setting the existing message ID resolves that case.

## Local preview and tests (optional)

Python 3.12+:

```sh
python -m pip install -r requirements.txt
python halo2_feed.py --dry-run
python -m unittest -v
```

Dry-run fetches the actual pages and prints the embed JSON; it does not contact
Discord and needs no webhook. Tests are offline and mock all Discord calls.
The Dockerfile uses Python 3.13. Local validation was performed on Python 3.12;
the Docker build and actual Railway/Discord delivery require your deployment.

## Reference documentation

- Insignia homepage: https://insignia.live/
- Halo 2 playlists and best-times chart: https://insignia.live/halo2
- Railway service source: https://docs.railway.com/services
- Railway GitHub deployments: https://docs.railway.com/deployments/github-autodeploys
- Railway persistent volumes: https://docs.railway.com/volumes
- Discord webhooks and editing messages: https://docs.discord.com/developers/resources/webhook
