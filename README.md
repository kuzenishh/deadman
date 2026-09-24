# deadman

Your agent was supposed to monitor something for the next 8 hours.

It died 20 minutes in. Nobody told you. You found out at midnight.

`deadman` watches for the **absence of a heartbeat**, not for a crash. A crash
you can catch. Silence you cannot. One file, no dependencies.

```
$ deadman watch scanner --every 30m --notify telegram
deadman: watching scanner, expects a ping every 30m (+5m grace)

$ deadman status
name                  every    last ping     state     pings
scanner               30m      2h00m ago     LATE      14

# and in Telegram:
[deadman] scanner is silent for 2h00m (expected every 30m).
last ping 2026-09-24 15:00:30Z.
```

## Install

```bash
curl -O https://raw.githubusercontent.com/kuzenishh/deadman/main/deadman.py
chmod +x deadman.py
```

Python 3.8+. Nothing to install, nothing to configure.

## Two ways to use it

### 1. Dead man's switch

Your job pings. If a ping does not arrive in time, you get told.

```bash
deadman watch scanner --every 30m --grace 5m --notify telegram
```

Then, from inside your loop:

```bash
deadman ping scanner
```

And once a minute from cron:

```
* * * * * /usr/local/bin/deadman check
```

This is the one that catches the case nobody catches: the process is still
alive, the logs look fine, and it silently stopped doing the work.

### 2. Supervisor

Run the thing, restart it, and tell you when it gives up.

```bash
deadman run scanner --every 30m --max-restarts 10 -- python3 scanner.py
```

Exponential backoff between restarts, capped. A run that survives longer than
`--reset-after` resets the backoff, so a job that crashes once an hour does not
end up waiting ten minutes to come back.

## Alerts

```bash
--notify telegram          # DEADMAN_TELEGRAM_TOKEN + DEADMAN_TELEGRAM_CHAT
--notify webhook           # DEADMAN_WEBHOOK_URL, posts JSON
--notify stderr            # default
--notify exec:'say dead'   # any shell command, gets $DEADMAN_TEXT
```

Repeatable, so you can have two channels. Check they actually work before you
rely on them:

```bash
deadman test scanner
```

It also tells you when the job comes back, so you are not left wondering.

## Commands

```
deadman watch NAME --every 30m    create or update a watch
deadman ping NAME                 heartbeat
deadman check                     alert on anything overdue (cron this)
deadman status                    table of every watch
deadman run NAME -- CMD           supervise and restart
deadman test NAME                 fire a test alert
deadman forget NAME               delete a watch
```

## What it doesn't do

No daemon. No agent. No server. No account. State is JSON files in
`~/.deadman/` that you can read and delete.

It does not try to restart your job from cron, does not parse your logs and
does not know what your job is supposed to be doing. It knows one thing: the
last time you said you were alive.

## Why

I build agents that run unattended. The failure that costs me is never the
crash, it is the silent stop: the process is up, nothing is in the error log,
and the work quietly stopped hours ago.

Every monitoring tool I looked at wanted a server, an account, or a YAML file.
This is 400 lines and a cron entry.

## License

MIT
