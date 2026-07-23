"""
Web admin dashboard (roadmap item #1).

Runs a small Flask app in a background thread inside the SAME process as
the live bot loop, so it shares the already-open Store connection and the
in-memory BotState/health module directly — no second service to deploy,
no extra DB connection pooling to worry about.

Read-only by default. The only mutating endpoints are the same operational
knobs already exposed over Telegram (/scannow, /startscan, /stopscan,
/enable, /disable, /setinterval) — nothing here can touch config.STRATEGY
or how a signal is generated. See commands.py for the Telegram equivalents;
both talk to the same BotState object, so a change from one shows up in
the other immediately.

Auth: everything (page + API) requires a token, either as ?token=... or
an X-Dashboard-Token header. If DASHBOARD_TOKEN isn't set in the
environment, a random one is generated at process start and pushed to
your Telegram admin chat (and logged) so you're never locked out but the
dashboard also isn't wide open by default. A successful token check sets
a signed session cookie (expires after DASHBOARD_SESSION_HOURS, default
12h) so you don't have to keep the query param around after the first
load. /logout clears it early if you want to.

CSRF: session cookies alone are not sufficient to call any state-changing
endpoint (scan-now, pause/resume, symbol enable/disable, interval) — those
also require a per-session CSRF token (double-submit pattern: a
`csrf_token` cookie plus a matching `X-CSRF-Token` header set by our own
JS). A third-party page can't read that cookie or set that header
cross-origin, so a stray <form> or <img> on some other site you have open
can't quietly pause your bot. GET/read endpoints only need the session.

This is a personal ops dashboard, not a multi-tenant product surface —
single shared token, no per-user accounts, no rate limiting beyond what
Railway/your network already gives you. Good enough for "only I can see
this," not good enough to put a paying customer's login flow behind.
"""
import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from . import health

logger = logging.getLogger(__name__)

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"


def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("take_profits", "confluence", "support_levels", "resistance_levels",
                "key_strengths", "key_concerns"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (TypeError, ValueError):
                pass
    return d


def create_app(store, bot_state, notifier, cfg) -> Flask:
    app = Flask(__name__)

    # Persist the Flask session secret in the same DB the rest of the bot
    # already uses, so logins survive a restart if a Volume is attached
    # (see README) instead of forcing a fresh /login every redeploy.
    secret = store.get_meta("dashboard_secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        store.set_meta("dashboard_secret_key", secret)
    app.secret_key = secret

    session_hours = getattr(cfg, "DASHBOARD_SESSION_HOURS", 12)
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(hours=session_hours),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        # Railway terminates TLS in front of the app; a plain HTTP local
        # run still needs the cookie to work, so only force Secure when
        # we can tell we're actually behind Railway/HTTPS.
        SESSION_COOKIE_SECURE=bool(getattr(cfg, "DASHBOARD_FORCE_SECURE_COOKIES", False)),
    )

    token = cfg.DASHBOARD_TOKEN
    if not token:
        token = secrets.token_urlsafe(18)
        msg = (
            f"🔑 Dashboard token (no DASHBOARD_TOKEN set, generated for this run):\n"
            f"{token}\n\n"
            f"Set DASHBOARD_TOKEN in your environment to keep this stable across restarts."
        )
        logger.warning(msg.replace("\n", " "))
        try:
            notifier.send_now(msg)
        except Exception:
            logger.exception("Could not send dashboard token to Telegram; check logs for it instead.")

    def _authed() -> bool:
        supplied = request.args.get("token") or request.headers.get("X-Dashboard-Token")
        if supplied and secrets.compare_digest(supplied, token):
            session.permanent = True
            session["dashboard_authed"] = True
            session.setdefault("csrf_token", secrets.token_urlsafe(24))
            return True
        return bool(session.get("dashboard_authed"))

    def _csrf_ok() -> bool:
        cookie_val = request.cookies.get(CSRF_COOKIE)
        header_val = request.headers.get(CSRF_HEADER)
        expected = session.get("csrf_token")
        return bool(expected and cookie_val and header_val
                    and secrets.compare_digest(cookie_val, expected)
                    and secrets.compare_digest(header_val, expected))

    def require_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _authed():
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("login", next=request.path))
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _csrf_ok():
                return jsonify({"error": "CSRF check failed — reload the page and try again"}), 403
            return view(*args, **kwargs)
        return wrapped

    @app.after_request
    def _security_headers(resp):
        # Prevent the dashboard token (when present in the URL right after
        # login) from leaking to the CDN via the Referer header, and stop
        # API responses (trade history, stats) from being cached anywhere.
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        if session.get("dashboard_authed") and session.get("csrf_token"):
            resp.set_cookie(
                CSRF_COOKIE, session["csrf_token"],
                samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"], httponly=False,
            )
        return resp

    @app.get("/login")
    def login():
        error = None
        if request.args.get("token"):
            if _authed():
                return redirect(request.args.get("next") or url_for("index"))
            error = "Invalid token."
        return render_template("login.html", error=error)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @require_auth
    def index():
        return render_template("dashboard.html", version=bot_state.version, symbols=bot_state.symbols)

    @app.get("/api/chart-analyses")
    @require_auth
    def api_chart_analyses():
        limit = min(int(request.args.get("limit", 20)), 200)
        rows = store.recent_chart_analyses(limit)
        return jsonify([_row_to_dict(r) for r in rows])

    # --- read-only API -----------------------------------------------------
    @app.get("/api/status")
    @require_auth
    def api_status():
        return jsonify({
            "version": bot_state.version,
            "scanning": bot_state.scanning.is_set(),
            "uptime": health.uptime_str(),
            "last_scan": health.last_scan_str(),
            "seconds_since_last_scan": round(health.seconds_since_last_scan()),
            "markets_scanned": health.markets_scanned(),
            "healthy": health.is_healthy(),
            "memory_mb": health.memory_mb(),
            "cpu_percent": health.cpu_percent(),
            "poll_seconds": bot_state.poll_seconds,
            "notify_cooldown_minutes": bot_state.notify_cooldown_minutes,
            "symbols": bot_state.symbols,
            "enabled_symbols": sorted(bot_state.enabled_symbols),
            "restart_count": int(store.get_meta("restart_count", 0) or 0),
            "now_utc": datetime.now(timezone.utc).isoformat(),
        })

    @app.get("/api/stats")
    @require_auth
    def api_stats():
        overall = store.performance_stats()
        per_symbol = store.performance_stats_by_symbol(bot_state.symbols)
        return jsonify({"overall": overall, "per_symbol": per_symbol})

    @app.get("/api/signals")
    @require_auth
    def api_signals():
        limit = min(int(request.args.get("limit", 50)), 500)
        rows = store.recent_signals(limit)
        return jsonify([_row_to_dict(r) for r in rows])

    @app.get("/api/errors")
    @require_auth
    def api_errors():
        limit = min(int(request.args.get("limit", 50)), 500)
        rows = store.recent_errors(limit)
        return jsonify([dict(r) for r in rows])

    @app.get("/api/equity")
    @require_auth
    def api_equity():
        # Cumulative R is a running total, so we can't just take the last
        # N rows in isolation without losing the correct starting offset —
        # compute the full running sum, then return only the tail the
        # chart actually renders. Keeps the response bounded regardless of
        # how many months of trade history have piled up.
        limit = min(int(request.args.get("limit", 1000)), 5000)
        rows = store.closed_signals_ordered()
        points = []
        cum_r = 0.0
        for r in rows:
            cum_r += r["outcome_r"] or 0.0
            points.append({
                "closed_at": r["closed_at"],
                "symbol": r["symbol"],
                "outcome_r": r["outcome_r"],
                "cumulative_r": round(cum_r, 3),
            })
        return jsonify(points[-limit:])

    # --- control endpoints (same scope as the existing Telegram commands) --
    @app.post("/api/scan-now")
    @require_auth
    def api_scan_now():
        bot_state.force_scan.set()
        return jsonify({"ok": True})

    @app.post("/api/scan/<action>")
    @require_auth
    def api_scan_toggle(action):
        if action == "pause":
            bot_state.scanning.clear()
        elif action == "resume":
            bot_state.scanning.set()
        else:
            return jsonify({"error": "action must be pause or resume"}), 400
        return jsonify({"ok": True, "scanning": bot_state.scanning.is_set()})

    @app.post("/api/symbol/<symbol>/<action>")
    @require_auth
    def api_symbol_toggle(symbol, action):
        symbol = symbol.upper()
        if symbol not in bot_state.symbols:
            return jsonify({"error": f"unknown symbol {symbol}"}), 404
        if action == "enable":
            bot_state.enabled_symbols.add(symbol)
        elif action == "disable":
            bot_state.enabled_symbols.discard(symbol)
        else:
            return jsonify({"error": "action must be enable or disable"}), 400
        return jsonify({"ok": True, "enabled_symbols": sorted(bot_state.enabled_symbols)})

    @app.post("/api/interval")
    @require_auth
    def api_interval():
        body = request.get_json(silent=True) or {}
        seconds = body.get("seconds")
        if not isinstance(seconds, int) or seconds < 5:
            return jsonify({"error": "seconds must be an integer >= 5"}), 400
        bot_state.poll_seconds = seconds
        store.set_meta("poll_seconds", seconds)
        return jsonify({"ok": True, "poll_seconds": seconds})

    return app


def start_dashboard(store, bot_state, notifier, cfg) -> threading.Thread:
    app = create_app(store, bot_state, notifier, cfg)

    def _run():
        try:
            from waitress import serve
            logger.info("Dashboard: serving with waitress on 0.0.0.0:%s", cfg.DASHBOARD_PORT)
            serve(app, host="0.0.0.0", port=cfg.DASHBOARD_PORT, _quiet=True)
        except ImportError:
            logger.warning(
                "Dashboard: waitress not installed, falling back to Flask's dev server "
                "(fine for a personal dashboard, not recommended for heavy traffic). "
                "`pip install waitress` to upgrade."
            )
            app.run(host="0.0.0.0", port=cfg.DASHBOARD_PORT, threaded=True, use_reloader=False)
        except Exception:
            logger.exception("Dashboard server crashed")

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    logger.info("Dashboard starting on port %s (DASHBOARD_ENABLED=%s)", cfg.DASHBOARD_PORT, cfg.DASHBOARD_ENABLED)
    return t
