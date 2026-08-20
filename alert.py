#!/usr/bin/env python3
"""
CoinDCX USDT/INR price alarm.
 
Polls CoinDCX's public ticker (no API key, no account access) and pushes an alert
when USDT/INR crosses a price you care about.
 
Sends to whichever channels you've configured:
  - Telegram  (free)      -> set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  - Pushover  ($5 once)   -> set PUSHOVER_TOKEN + PUSHOVER_USER
Both work on phone and laptop at the same time.
 
Usage:
    python alert.py                 # one shot (what GitHub Actions runs)
    python alert.py --loop          # run forever, for an always-on PC
    python alert.py --test          # send a test message to prove it works
    python alert.py --price 91.5    # simulate a price, to check your rules
    python alert.py --price 91.5 --dry-run   # print instead of sending
"""
 
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
 
TICKER_URL = "https://api.coindcx.com/exchange/ticker"
MARKET = "USDTINR"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
IST = timezone(timedelta(hours=5, minutes=30))
 
 
# ---------------------------------------------------------------- helpers
 
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
 
 
def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
 
 
def post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "usdt-alert/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)
 
 
def get_price(market=MARKET):
    """Return last traded price for a market from CoinDCX's public ticker."""
    req = urllib.request.Request(TICKER_URL, headers={"User-Agent": "usdt-alert/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    for row in data:
        if row.get("market") == market:
            return float(row["last_price"])
    raise RuntimeError(f"Market {market} not found in CoinDCX ticker response")
 
 
# ---------------------------------------------------------------- channels
 
def send_telegram(text_html, urgent=True):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return None
    body = post(f"https://api.telegram.org/bot{token}/sendMessage", {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_notification": "false" if urgent else "true",
    })
    if not body.get("ok"):
        raise RuntimeError(f"Telegram error: {body}")
    return "telegram"
 
 
def send_pushover(text_plain, title="USDT/INR alert", urgent=True, config=None):
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not (token and user):
        return None
    config = config or {}
    fields = {
        "token": token,
        "user": user,
        "title": title,
        "message": text_plain,
    }
    if urgent and config.get("pushover_emergency", True):
        # priority 2 = repeat until you tap "acknowledge" on the phone
        fields["priority"] = "2"
        fields["retry"] = str(int(config.get("pushover_retry_seconds", 60)))
        fields["expire"] = str(int(config.get("pushover_expire_seconds", 1800)))
        fields["sound"] = config.get("pushover_sound", "persistent")
    body = post("https://api.pushover.net/1/messages.json", fields)
    if body.get("status") != 1:
        raise RuntimeError(f"Pushover error: {body}")
    return "pushover"
 
 
def send_all(text_html, text_plain, urgent=True, config=None):
    sent = []
    errors = []
    for fn, arg in ((send_telegram, text_html), (send_pushover, text_plain)):
        try:
            r = fn(arg, urgent=urgent) if fn is send_telegram else \
                fn(arg, urgent=urgent, config=config)
            if r:
                sent.append(r)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    if not sent:
        raise RuntimeError("No alert channel configured or all failed. " +
                           ("; ".join(errors) if errors else
                            "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."))
    if errors:
        print("warning: " + "; ".join(errors), file=sys.stderr)
    return sent
 
 
def send_burst(text_html, text_plain, config=None, sleeper=time.sleep):
    """Ring several times in a row so one missed buzz doesn't mean a missed alert.
 
    A single workflow run fires the whole burst, so all of it lands within a couple
    of minutes rather than being spread over the 5-minute cron.
 
    Pushover in emergency mode already repeats until you acknowledge, so it only
    gets the first message of the burst -- no point stacking sirens.
    """
    config = config or {}
    count = max(1, int(config.get("burst_count", 5)))
    gap = max(0, int(config.get("burst_gap_seconds", 30)))
    channels = []
 
    for i in range(1, count + 1):
        # A counter makes each message unique, so iOS won't silently collapse
        # them into one grouped notification with a single sound.
        suffix_html = f"\n\n<i>ring {i} of {count}</i>" if count > 1 else ""
        suffix_plain = f"\n\nring {i} of {count}" if count > 1 else ""
 
        if i == 1:
            channels = send_all(text_html + suffix_html,
                                text_plain + suffix_plain, True, config)
        else:
            try:
                send_telegram(text_html + suffix_html, urgent=True)
            except Exception as e:
                print(f"warning: burst {i}: {e}", file=sys.stderr)
 
        if i < count and gap:
            sleeper(gap)
 
    return channels, count
 
 
# ---------------------------------------------------------------- core
 
def evaluate(price, config, state):
    """Return (list of (html, plain) messages to send, updated state)."""
    hysteresis = float(config.get("hysteresis_inr", 0.10))
    max_repeats = int(config.get("max_repeats", 3))
    messages = []
 
    for rule in config["rules"]:
        if not rule.get("enabled", True):
            continue
 
        rid = rule["id"]
        target = float(rule["price"])
        direction = rule["direction"]          # "above" or "below"
        st = state.setdefault(rid, {"triggered": False, "repeats": 0})
 
        if direction == "above":
            hit = price >= target
            rearm = price < target - hysteresis
        elif direction == "below":
            hit = price <= target
            rearm = price > target + hysteresis
        else:
            raise ValueError(f"rule {rid}: direction must be 'above' or 'below'")
 
        if hit:
            if not st["triggered"]:
                st["triggered"] = True
                st["repeats"] = 1
                messages.append(format_alert(rule, price, first=True))
            elif config.get("repeat_until_rearm", True) and st["repeats"] < max_repeats:
                st["repeats"] += 1
                messages.append(format_alert(rule, price, first=False,
                                             n=st["repeats"], of=max_repeats))
        elif rearm and st["triggered"]:
            st["triggered"] = False
            st["repeats"] = 0
 
    return messages, state
 
 
def format_alert(rule, price, first=True, n=None, of=None):
    arrow = "▲" if rule["direction"] == "above" else "▼"
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    head = "USDT/INR ALERT" if first else f"STILL ACTIVE ({n}/{of})"
    note = rule.get("note", "")
    target = float(rule["price"])
 
    html = [f"\U0001f6a8 <b>{head}</b>", "",
            f"{arrow} USDT is now <b>₹{price:.4f}</b>",
            f"Your trigger: {rule['direction']} ₹{target:.4f}"]
    plain = [f"{arrow} USDT is now Rs {price:.4f}",
             f"Your trigger: {rule['direction']} Rs {target:.4f}"]
    if note:
        html += ["", f"<i>{note}</i>"]
        plain += ["", note]
    html += ["", now]
    plain += ["", now]
    return "\n".join(html), "\n".join(plain)
 
 
# ---------------------------------------------------------------- runner
 
def run_once(config, forced_price=None, dry_run=False):
    price = forced_price if forced_price is not None else get_price(
        config.get("market", MARKET))
    state = load_json(STATE_PATH, {})
    messages, state = evaluate(price, config, state)
 
    state["_last"] = {
        "price": price,
        "checked_at": datetime.now(IST).isoformat(timespec="seconds"),
    }
    save_json(STATE_PATH, state)
 
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] USDT/INR = {price:.4f} "
          f"-> {len(messages)} alert(s)")
 
    for html, plain in messages:
        if dry_run:
            n = max(1, int(config.get("burst_count", 5)))
            gap = int(config.get("burst_gap_seconds", 30))
            print(f"--- would send {n}x, {gap}s apart ---")
            print(plain)
        else:
            channels, n = send_burst(html, plain, config)
            print(f"rang {n}x via: " + ", ".join(channels))
    return price, messages
 
 
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="poll forever")
    ap.add_argument("--interval", type=int, default=None, help="seconds between polls")
    ap.add_argument("--test", action="store_true", help="send a test message")
    ap.add_argument("--price", type=float, default=None, help="simulate a price")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = ap.parse_args()
 
    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"Missing or invalid {CONFIG_PATH}")
 
    if args.test:
        channels, n = send_burst(
            "✅ <b>USDT/INR alarm is wired up.</b>\n\n"
            "If this woke you, your sound settings are right.",
            "USDT/INR alarm is wired up.\n\n"
            "If this woke you, your sound settings are right.",
            config=config)
        print(f"Test rang {n}x via: " + ", ".join(channels))
        return
 
    interval = args.interval or int(config.get("poll_seconds", 300))
 
    if args.loop:
        while True:
            try:
                run_once(config, args.price, args.dry_run)
            except Exception as e:                      # keep the loop alive
                print(f"error: {e}", file=sys.stderr)
            time.sleep(interval)
    else:
        run_once(config, args.price, args.dry_run)
 
 
if __name__ == "__main__":
    main()
