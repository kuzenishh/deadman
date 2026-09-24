#!/usr/bin/env python3
"""deadman - a dead man's switch for long-running agents and jobs.

Your agent is supposed to be monitoring something for the next 8 hours.
It died 20 minutes in. Nobody told you.

deadman watches for the absence of a heartbeat, not for a crash. Single file,
no dependencies.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
HOME = Path(os.environ.get("DEADMAN_HOME", Path.home() / ".deadman"))
TIMEOUT = 15

DURATION_RE = re.compile(r"^(\d+)([smhd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def parse_duration(text: str) -> int:
    """'30m' -> 1800. Accepts s, m, h, d."""
    m = DURATION_RE.match(text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"bad duration {text!r}, use forms like 90s, 30m, 8h, 1d"
        )
    return int(m.group(1)) * UNITS[m.group(2)]


def human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        sys.exit("deadman: name must be 1-64 chars of letters, digits, . _ -")
    return name


def path_for(name: str) -> Path:
    return HOME / f"{name}.json"


def load(name: str) -> dict:
    p = path_for(name)
    if not p.exists():
        sys.exit(f"deadman: no watch named {name!r}. Run: deadman watch {name} --every 30m")
    return json.loads(p.read_text(encoding="utf-8"))


def store(data: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    p = path_for(data["name"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def all_watches() -> list:
    if not HOME.is_dir():
        return []
    out = []
    for p in sorted(HOME.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


# --------------------------------------------------------------------------- #
# notifications


def notify(watch: dict, text: str, verbose: bool = False) -> bool:
    """Send an alert. Returns True if at least one channel accepted it."""
    sent = False
    channels = watch.get("notify") or ["stderr"]

    for channel in channels:
        try:
            if channel == "telegram":
                token = os.environ.get("DEADMAN_TELEGRAM_TOKEN")
                chat = os.environ.get("DEADMAN_TELEGRAM_CHAT")
                if not token or not chat:
                    print("deadman: telegram env vars not set, skipping", file=sys.stderr)
                    continue
                body = urllib.parse.urlencode(
                    {"chat_id": chat, "text": text}
                ).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage", data=body
                )
                urllib.request.urlopen(req, timeout=TIMEOUT).read()
                sent = True

            elif channel == "webhook":
                url = os.environ.get("DEADMAN_WEBHOOK_URL")
                if not url:
                    print("deadman: DEADMAN_WEBHOOK_URL not set, skipping", file=sys.stderr)
                    continue
                body = json.dumps({"text": text, "watch": watch["name"]}).encode()
                req = urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"}
                )
                urllib.request.urlopen(req, timeout=TIMEOUT).read()
                sent = True

            elif channel.startswith("exec:"):
                cmd = channel[5:]
                subprocess.run(
                    cmd, shell=True, timeout=TIMEOUT,
                    env={**os.environ, "DEADMAN_TEXT": text, "DEADMAN_NAME": watch["name"]},
                )
                sent = True

            else:  # stderr
                print(text, file=sys.stderr)
                sent = True

        except (urllib.error.URLError, subprocess.SubprocessError, OSError) as exc:
            print(f"deadman: notify via {channel} failed: {exc}", file=sys.stderr)

    return sent


# --------------------------------------------------------------------------- #
# commands


def cmd_watch(args):
    name = safe_name(args.name)
    existing = path_for(name)
    data = json.loads(existing.read_text(encoding="utf-8")) if existing.exists() else {}
    data.update({
        "name": name,
        "every": args.every,
        "grace": args.grace,
        "notify": args.notify,
        "note": args.note or data.get("note", ""),
        "created": data.get("created", now()),
        "last_ping": data.get("last_ping"),
        "alerted": data.get("alerted", False),
        "pings": data.get("pings", 0),
    })
    store(data)
    print(f"deadman: watching {name}, expects a ping every {human(args.every)} "
          f"(+{human(args.grace)} grace)")


def cmd_ping(args):
    name = safe_name(args.name)
    data = load(name)
    was_late = data.get("alerted")
    data["last_ping"] = now()
    data["pings"] = data.get("pings", 0) + 1
    data["alerted"] = False
    store(data)
    if was_late:
        notify(data, f"[deadman] {name} is back. Ping received at {iso(now())}.")
        print(f"deadman: {name} recovered")
    else:
        print(f"deadman: {name} ok ({data['pings']} pings)")


def cmd_check(args):
    watches = all_watches()
    if not watches:
        print("deadman: nothing to check")
        return
    late = 0
    for data in watches:
        if args.name and data["name"] != args.name:
            continue
        deadline = data["every"] + data["grace"]
        last = data.get("last_ping")
        if last is None:
            silence = now() - data["created"]
            source = "never pinged since it was created"
        else:
            silence = now() - last
            source = f"last ping {iso(last)}"
        if silence <= deadline:
            continue
        late += 1
        if data.get("alerted") and not args.repeat:
            continue
        text = (f"[deadman] {data['name']} is silent for {human(silence)} "
                f"(expected every {human(data['every'])}). {source}.")
        if data.get("note"):
            text += f"\n{data['note']}"
        notify(data, text)
        data["alerted"] = True
        store(data)
    print(f"deadman: checked {len(watches)}, {late} overdue")
    if late and args.exit_code:
        sys.exit(2)


def cmd_status(args):
    watches = all_watches()
    if not watches:
        print("deadman: no watches yet")
        return
    print(f"{'name':<22}{'every':<9}{'last ping':<14}{'state':<10}pings")
    for d in watches:
        last = d.get("last_ping")
        deadline = d["every"] + d["grace"]
        if last is None:
            ago, silence = "never", now() - d["created"]
        else:
            silence = now() - last
            ago = human(silence) + " ago"
        state = "LATE" if silence > deadline else "ok"
        print(f"{d['name']:<22}{human(d['every']):<9}{ago:<14}{state:<10}{d.get('pings', 0)}")


def cmd_run(args):
    """Supervise a command: restart it, ping on each start, alert when it gives up."""
    name = safe_name(args.name)
    if not path_for(name).exists():
        cmd_watch(argparse.Namespace(
            name=name, every=args.every, grace=args.grace,
            notify=args.notify, note=f"supervising: {' '.join(args.command)}"))
    data = load(name)

    delay = args.backoff
    restarts = 0
    stopping = {"flag": False}

    def on_signal(signum, frame):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while not stopping["flag"]:
        started = now()
        print(f"deadman: starting {shlex.join(args.command)}")
        try:
            code = subprocess.call(args.command)
        except FileNotFoundError:
            sys.exit(f"deadman: command not found: {args.command[0]}")

        data = load(name)
        data["last_ping"] = now()
        data["pings"] = data.get("pings", 0) + 1
        data["alerted"] = False
        store(data)

        ran_for = now() - started
        if stopping["flag"]:
            break
        if code == 0 and not args.always:
            print(f"deadman: {name} exited cleanly after {human(ran_for)}")
            return

        restarts += 1
        if args.max_restarts and restarts > args.max_restarts:
            notify(data, f"[deadman] {name} gave up after {restarts - 1} restarts. "
                         f"Last exit code {code}.")
            sys.exit(1)

        if ran_for >= args.reset_after:
            delay = args.backoff  # it survived a while, reset the backoff
        print(f"deadman: {name} exited with {code} after {human(ran_for)}, "
              f"restart {restarts} in {human(delay)}")
        for _ in range(int(delay)):
            if stopping["flag"]:
                break
            time.sleep(1)
        delay = min(delay * 2, args.max_backoff)

    print(f"deadman: {name} stopped by signal")


def cmd_forget(args):
    name = safe_name(args.name)
    p = path_for(name)
    if not p.exists():
        sys.exit(f"deadman: no watch named {name!r}")
    p.unlink()
    print(f"deadman: forgot {name}")


def cmd_test(args):
    data = load(safe_name(args.name))
    ok = notify(data, f"[deadman] test alert for {data['name']}. If you see this, alerts work.")
    print("deadman: sent" if ok else "deadman: nothing was sent, check your channels")


# --------------------------------------------------------------------------- #


def build_parser():
    p = argparse.ArgumentParser(
        prog="deadman",
        description="A dead man's switch for long-running agents and jobs.",
    )
    p.add_argument("--version", action="version", version=f"deadman {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--every", type=parse_duration, default=1800,
                        help="expected heartbeat interval, e.g. 30m (default 30m)")
        sp.add_argument("--grace", type=parse_duration, default=300,
                        help="extra slack before alerting (default 5m)")
        sp.add_argument("--notify", action="append", default=None,
                        help="telegram | webhook | stderr | exec:<command>, repeatable")

    w = sub.add_parser("watch", help="create or update a watch")
    w.add_argument("name")
    w.add_argument("--note", help="text added to the alert")
    add_common(w)
    w.set_defaults(fn=cmd_watch)

    pg = sub.add_parser("ping", help="tell deadman the job is still alive")
    pg.add_argument("name")
    pg.set_defaults(fn=cmd_ping)

    c = sub.add_parser("check", help="alert on anything overdue (run this from cron)")
    c.add_argument("--name", help="check one watch only")
    c.add_argument("--repeat", action="store_true", help="alert again every check")
    c.add_argument("--exit-code", action="store_true", help="exit 2 if anything is overdue")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="show every watch")
    s.set_defaults(fn=cmd_status)

    r = sub.add_parser("run", help="supervise a command and restart it")
    r.add_argument("name")
    r.add_argument("--max-restarts", type=int, default=0, help="0 means unlimited")
    r.add_argument("--backoff", type=parse_duration, default=5,
                   help="first restart delay (default 5s)")
    r.add_argument("--max-backoff", type=parse_duration, default=600,
                   help="cap on the restart delay (default 10m)")
    r.add_argument("--reset-after", type=parse_duration, default=300,
                   help="a run longer than this resets the backoff (default 5m)")
    r.add_argument("--always", action="store_true",
                   help="restart even after a clean exit")
    add_common(r)
    r.add_argument("command", nargs="*", default=[],
                   help="the command, after --")
    r.set_defaults(fn=cmd_run)

    f = sub.add_parser("forget", help="delete a watch")
    f.add_argument("name")
    f.set_defaults(fn=cmd_forget)

    t = sub.add_parser("test", help="fire a test alert")
    t.add_argument("name")
    t.set_defaults(fn=cmd_test)

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after a standalone "--" is the supervised command, not our flags.
    tail = []
    if "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if getattr(args, "cmd", None) == "run":
        args.command = tail or args.command
        if not args.command:
            sys.exit("deadman: give a command after --, "
                     "e.g. deadman run job --every 30m -- python worker.py")
    args.fn(args)


if __name__ == "__main__":
    main()
