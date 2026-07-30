"""Web Push notifications — the app is its own application server.

There is exactly one notification: **no basal dose has been logged for a while**.
Basal is the dose that is easy to forget and, unlike a bolus, invisible in the
glucose trace until hours later. Note what is being said, because the wording in
here has to stay honest to it: the notification reports a gap in the *log*. It
never tells you to take insulin. Sugar Daddy is a record-keeping tool, not a
medical device — see the framing in CLAUDE.md and docs/plans/insulin-awareness.md.

Push works without any third-party notification account: this box holds a VAPID
keypair and signs and encrypts every message itself. The browser's push service
(FCM on Android) only *relays* an already-encrypted blob, so the relay never sees
the payload — it is encrypted end-to-end under keys derived per subscription
(RFC 8291). The relay itself can't be swapped out; it's baked into the browser.

The private key is a secret, so it comes from ``SUGARDADDY_VAPID_PRIVATE_KEY``
only, never the TOML (see config.py). The matching public key is *derived* from it
rather than configured separately, so the two can never drift apart and a rotated
key takes effect everywhere at once. Mint one with `sugardaddy vapid-keys`.

pywebpush/cryptography are imported lazily per function so the rest of the app —
and the plain-assert tests, which run under a bare interpreter — keep working
without those wheels installed. The decision of *whether* a basal is overdue is
not here either: that is pure maths, and lives in `analysis.basal_status`.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from datetime import datetime, tzinfo

from sugardaddy.analysis import basal_status
from sugardaddy.config import Config, load_config
from sugardaddy.db import Database

log = logging.getLogger("sugardaddy.notify")

# Push services reject anything much larger, and we only ever send a tiny JSON
# object, so this is just a guard against a pathological note.
_MAX_BODY_CHARS = 300

# Per-subscription HTTP timeout. A wedged push service must not hold up the pass
# (or, in the web app, the /api/push/test request).
_SEND_TIMEOUT = 10

# One notification replaces the previous one rather than stacking a pile of
# identical nags in the shade.
_TAG = "sugardaddy-basal"

# notify_state keys. The anchor is the timestamp of the basal dose the last
# notification was about, which is what makes "once per missed dose" fall out of
# the arithmetic: logging a basal moves the anchor and re-arms the reminder.
_K_ANCHOR = "basal_notified_anchor"
_K_AT = "basal_notified_at"


class PushError(RuntimeError):
    """Raised when push is asked for but cannot be done (missing deps or key)."""


# --- key handling ------------------------------------------------------------


def _b64(raw: bytes) -> str:
    """base64url without padding — the encoding every Web Push API expects."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise PushError("push needs pywebpush installed (pip install -e .)") from exc
    return ec, serialization


def generate_private_key() -> str:
    """Return a fresh P-256 private key as the base64url raw scalar Web Push uses."""
    ec, _ = _crypto()
    key = ec.generate_private_key(ec.SECP256R1())
    return _b64(key.private_numbers().private_value.to_bytes(32, "big"))


def public_key_for(private_b64: str) -> str:
    """Derive the base64url ``applicationServerKey`` the browser subscribes with."""
    ec, serialization = _crypto()
    value = int.from_bytes(_unb64(private_b64.strip()), "big")
    key = ec.derive_private_key(value, ec.SECP256R1())
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64(raw)


def public_key(cfg: Config) -> str:
    """The public key to hand the browser, or "" when push isn't usable.

    Returns empty rather than raising so the app still starts and serves the UI
    when notifications are off or misconfigured — the phone's toggle just reports
    itself unavailable.
    """
    if not cfg.notify.enabled or not cfg.vapid_private_key:
        return ""
    try:
        return public_key_for(cfg.vapid_private_key)
    except Exception as exc:  # bad key material shouldn't take the app down
        log.error("cannot derive VAPID public key: %s", exc)
        return ""


# --- sending -----------------------------------------------------------------


def send_to_all(db: Database, cfg: Config, payload: dict, now: int) -> dict:
    """Deliver one payload to every stored subscription.

    Push services answer 404/410 for a subscription the browser has discarded
    (app uninstalled, data cleared). That is permanent, so those rows are pruned
    immediately — otherwise dead endpoints accumulate forever and every pass pays
    to retry them. Everything else, including a push service we simply couldn't
    reach, is transient and only bumps a counter.

    Returns a summary dict. One dead subscription never stops the others being
    tried, and no delivery failure raises.
    """
    if not cfg.notify.enabled:
        raise PushError("notifications are disabled in config ([notify] enabled)")
    if not cfg.vapid_private_key:
        raise PushError("SUGARDADDY_VAPID_PRIVATE_KEY is not set")

    from pywebpush import WebPushException, webpush

    subs = db.list_subscriptions()
    sent = pruned = failed = 0
    data = json.dumps(payload)
    claims = {"sub": cfg.notify.subject}

    for sub in subs:
        try:
            webpush(
                subscription_info=sub.to_info(),
                data=data,
                vapid_private_key=cfg.vapid_private_key,
                # py_vapid mutates the claims dict (it stamps `exp`), so hand each
                # send its own copy.
                vapid_claims=dict(claims),
                ttl=cfg.notify.ttl_seconds,
                timeout=_SEND_TIMEOUT,
            )
            db.mark_subscription_ok(sub.id, now)
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                db.delete_subscription(sub.endpoint)
                pruned += 1
                log.info("pruned expired push subscription %s", sub.id)
            else:
                db.mark_subscription_failed(sub.id)
                failed += 1
                log.warning("push to subscription %s failed (%s): %s", sub.id, status, exc)
        except Exception as exc:
            # Transport-level trouble (DNS, TLS, timeout) arrives as a bare
            # requests error, not a WebPushException. It says nothing about this
            # subscription's validity, so retry it next pass.
            db.mark_subscription_failed(sub.id)
            failed += 1
            log.warning("push to subscription %s errored: %s", sub.id, exc)

    return {"subscriptions": len(subs), "sent": sent, "pruned": pruned, "failed": failed}


# --- the notify pass ---------------------------------------------------------


def humanize_gap(hours: float) -> str:
    """The gap since the last basal, in the roughest unit that still reads true.

    Deliberately coarse. "25 h" is the useful fact; "25.4 h" implies the reminder
    knows something it doesn't, since the threshold is a rule of thumb.
    """
    if hours < 48:
        return f"{round(hours)} h"
    days = hours / 24
    return f"{days:.0f} days" if round(days) != 1 else "1 day"


def build_payload(status: dict, tz: tzinfo, *, repost: bool = False) -> dict:
    """Shape the one notification this app sends.

    Phrased as a fact about the log ("no basal dose logged") rather than an
    instruction ("take your basal"), and it names when the last one was so the
    reminder can be dismissed on sight when it's wrong — you took it and forgot to
    log it, which is a different problem from not taking it.

    A `repost` — the same unlogged dose pushed again under `repeat_hours` — is
    silent and never re-alerts. The first notification of a cycle gets your
    attention; after that it should just sit there, reappearing if you swipe it
    away rather than buzzing again.
    """
    gap = humanize_gap(status["hours_since"] or 0)
    last = datetime.fromtimestamp(status["last_ts"], tz).strftime("%a %H:%M")
    return {
        "title": "Basal not logged",
        "body": f"No basal dose logged for {gap} — last one was {last}."[:_MAX_BODY_CHARS],
        "url": "/",
        "tag": _TAG,
        "silent": bool(repost),
        "renotify": not repost,
    }


def run_pass(
    db: Database, cfg: Config, now: int, tz: tzinfo, *, dry_run: bool = False
) -> dict:
    """Notify if a basal dose is overdue in the log and we haven't just said so.

    Two gates sit between "overdue" and "push", both keyed on `notify_state`:

    * a *new* anchor (a different last-basal dose than the one last notified
      about) always notifies — that is the once-per-missed-dose rule, and logging
      a basal is what re-arms it;
    * the same anchor only repeats once `repeat_hours` has passed, and quietly.

    State is stamped only when a delivery actually succeeded. If every send failed
    (server offline, push service hiccup) the stamp is left alone so the next pass
    retries instead of silently swallowing the reminder. With no subscriptions at
    all nothing is stamped either, so a device that subscribes later still hears
    about an outstanding dose.
    """
    last = db.latest_dose_of_kind("basal")
    status = basal_status(
        last.ts_utc if last else None,
        now,
        interval_hours=cfg.notify.basal_interval_hours,
        leniency_hours=cfg.notify.basal_leniency_hours,
    )
    result: dict = {"due": status["due"], "status": status, "sent": 0}
    if not status["due"]:
        return result

    anchor = str(status["last_ts"])
    prev_anchor = db.get_state(_K_ANCHOR)
    repost = anchor == prev_anchor
    if repost:
        # Already told about this one. Only speak again on the repeat cadence.
        if not cfg.notify.repeat_hours:
            return result
        try:
            notified_at = int(db.get_state(_K_AT) or 0)
        except ValueError:  # corrupt stamp: treat as never notified
            notified_at = 0
        if now - notified_at < cfg.notify.repeat_hours * 3600:
            return result
    result["repost"] = repost

    payload = build_payload(status, tz, repost=repost)
    if dry_run:
        result["payload"] = payload
        return result

    if not db.list_subscriptions():
        log.info("basal overdue by %.1f h but no push subscriptions registered",
                 status["overdue_hours"])
        return result

    outcome = send_to_all(db, cfg, payload, now)
    result.update(outcome)
    if outcome["sent"]:
        db.set_state(_K_ANCHOR, anchor)
        db.set_state(_K_AT, str(now))
    return result


# --- background loop (used by `serve`) ---------------------------------------


def notify_loop(
    cfg: Config, db: Database, tz: tzinfo, stop: threading.Event | None = None
) -> None:
    """Check on an interval until `stop` is set (or forever if None).

    Mirrors the ingest loop: it never dies on error, because a push service having
    a bad afternoon must not cost you every future reminder. First check happens
    after one interval rather than at startup, so a restart loop can't turn into a
    burst of notifications.
    """
    interval = max(60, cfg.notify.poll_seconds)

    def sleep(seconds: float) -> bool:
        """Sleep, but wake early if asked to stop. Returns True if stopping."""
        if stop is None:
            time.sleep(seconds)
            return False
        return stop.wait(seconds)

    while True:
        if sleep(interval):
            log.info("notify loop stopping")
            return
        try:
            result = run_pass(db, cfg, int(time.time()), tz)
            if result.get("sent"):
                log.info("basal reminder pushed: %s", result)
        except Exception as exc:  # never let the loop die
            log.error("notify pass failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))


def start_background(
    cfg: Config, db: Database, tz: tzinfo
) -> tuple[threading.Thread, threading.Event]:
    """Spawn the notify loop as a daemon thread (used by `serve`)."""
    stop = threading.Event()
    t = threading.Thread(
        target=notify_loop, args=(cfg, db, tz, stop), name="notify", daemon=True
    )
    t.start()
    return t, stop


def run_notify(config_path: str, *, dry_run: bool = False) -> int:
    """The `sugardaddy notify` command: one pass, then report what happened."""
    from zoneinfo import ZoneInfo

    cfg = load_config(config_path)
    db = Database(cfg.database.path)
    db.init_db()
    try:
        tz = ZoneInfo(cfg.web.timezone)
    except Exception:  # pragma: no cover - bad tz name / missing tzdata
        from datetime import timezone as _utc

        log.warning("unknown timezone %r; using UTC", cfg.web.timezone)
        tz = _utc.utc

    try:
        result = run_pass(db, cfg, int(time.time()), tz, dry_run=dry_run)
    except PushError as exc:
        print(f"error: {exc}")
        return 2

    status = result["status"]
    if status["last_ts"] is None:
        print("no basal dose has ever been logged — nothing to remind about")
        return 0
    if not result["due"]:
        print(f"last basal {humanize_gap(status['hours_since'])} ago — not due yet")
        return 0

    print(f"basal overdue: last logged {humanize_gap(status['hours_since'])} ago, "
          f"{status['overdue_hours']:.1f} h past the reminder threshold")
    # `repost` is only set once both gates are cleared, so its absence means this
    # dose has already been notified about and the repeat interval hasn't elapsed.
    if "repost" not in result:
        print("already notified about this one; waiting for the repeat interval")
        return 0
    if dry_run:
        print(f"would send: {json.dumps(result.get('payload', {}), indent=2)}")
        return 0
    print(f"sent {result.get('sent', 0)}/{result.get('subscriptions', 0)} subscription(s), "
          f"pruned {result.get('pruned', 0)}, failed {result.get('failed', 0)}")
    return 0


def run_vapid_keys() -> int:
    """Mint a keypair. Only the private key is stored — the public one is derived
    from it at runtime, so there is nothing to keep in sync."""
    try:
        private = generate_private_key()
    except PushError as exc:
        print(f"error: {exc}")
        return 2
    print("Add this to the server's environment (never to config.toml):\n")
    print(f"  SUGARDADDY_VAPID_PRIVATE_KEY={private}\n")
    print(f"Derived public key (served at /api/push/key): {public_key_for(private)}")
    return 0
