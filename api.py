"""
Icebreaker 345 Pro - Unified Flask Backend

SINGLE PRODUCTION SERVER containing:
- LotteryUSA Scraper
- Chris Pair Predictor Engine
- Watch/Pressure Analysis
- Device Registration (SQLite)
- APNs Push Notifications
- maybe_notify() pipeline

Production: gunicorn api:app
"""

import os
import json
import sqlite3
import hashlib
import time
import threading
from datetime import datetime
from functools import wraps
from typing import List, Dict, Optional, Any
from dataclasses import asdict

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import jwt
import requests

from chris_pair_predictor import (
    run_chris_decoder, 
    analyze_number, 
    ChrisAnalysis,
    CHRIS_PAIRS
)
from lottery_scraper import (
    fetch_state, 
    fetch_all_states, 
    LotteryResult,
    generate_fingerprint,
    get_state_info
)

# Load environment variables
load_dotenv()

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)
CORS(app)

# Configuration
DATABASE_PATH = os.getenv("DATABASE_PATH", "icebreaker.db")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "15"))

# APNs Configuration
APNS_KEY_ID = os.getenv("APNS_KEY_ID", "")
APNS_TEAM_ID = os.getenv("APNS_TEAM_ID", "")
APNS_KEY_PATH = os.getenv("APNS_KEY_PATH", "")
APNS_BUNDLE_ID = os.getenv("APNS_BUNDLE_ID", "com.app.icebreaker345pro")
APNS_PRODUCTION = os.getenv("APNS_PRODUCTION", "false").lower() == "true"


# ============================================================
# DATABASE
# ============================================================

def get_db():
    """Get database connection for current request."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    """Close database connection."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Initialize database tables."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    # Lottery results
    c.execute("""
        CREATE TABLE IF NOT EXISTS lottery_results (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            state_code TEXT NOT NULL,
            game TEXT NOT NULL,
            draw TEXT NOT NULL,
            number TEXT NOT NULL,
            draw_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_results_fingerprint ON lottery_results(fingerprint)")
    
    # Chris analyses
    c.execute("""
        CREATE TABLE IF NOT EXISTS chris_analyses (
            id TEXT PRIMARY KEY,
            result_id TEXT,
            source_number TEXT NOT NULL,
            state TEXT,
            state_code TEXT,
            game TEXT,
            draw TEXT,
            draw_date TEXT,
            pattern TEXT NOT NULL,
            triggered_rules TEXT NOT NULL,
            watch_numbers TEXT NOT NULL,
            pressure_type TEXT NOT NULL,
            total_signals INTEGER NOT NULL,
            predictions TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    
    # Registered devices
    c.execute("""
        CREATE TABLE IF NOT EXISTS registered_devices (
            id TEXT PRIMARY KEY,
            device_token TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL DEFAULT 'ios',
            app_version TEXT DEFAULT '1.0.0',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notify_pair_follower INTEGER DEFAULT 1,
            notify_double_pressure INTEGER DEFAULT 1,
            notify_triple_pressure INTEGER DEFAULT 1,
            notify_quad_pressure INTEGER DEFAULT 1,
            minimum_signal_count INTEGER DEFAULT 1,
            enabled_states TEXT,
            enabled_games TEXT,
            is_active INTEGER DEFAULT 1,
            invalid_token_at TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_devices_token ON registered_devices(device_token)")
    
    # Notification sent log (prevents duplicates)
    c.execute("""
        CREATE TABLE IF NOT EXISTS notification_sent_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            result_id TEXT NOT NULL,
            analysis_id TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE,
            sent_at TEXT NOT NULL,
            success INTEGER NOT NULL,
            error_message TEXT,
            apns_id TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sent_fingerprint ON notification_sent_log(fingerprint)")
    
    conn.commit()
    conn.close()
    print("Database initialized")


# ============================================================
# AUTH DECORATOR
# ============================================================

def require_admin(f):
    """Require admin API key for endpoint."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not ADMIN_API_KEY or api_key != ADMIN_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ============================================================
# APNs PUSH NOTIFICATIONS
# ============================================================

_apns_jwt_token = None
_apns_jwt_expiry = 0
_apns_private_key = None


def load_apns_key():
    """Load APNs private key from file."""
    global _apns_private_key
    if APNS_KEY_PATH and os.path.exists(APNS_KEY_PATH):
        with open(APNS_KEY_PATH, 'r') as f:
            _apns_private_key = f.read()
        print(f"APNs key loaded from {APNS_KEY_PATH}")
    else:
        print("Warning: APNs key not configured")


def get_apns_jwt() -> Optional[str]:
    """Get or refresh APNs JWT token."""
    global _apns_jwt_token, _apns_jwt_expiry
    
    if not _apns_private_key or not APNS_KEY_ID or not APNS_TEAM_ID:
        return None
    
    now = int(time.time())
    if _apns_jwt_token and _apns_jwt_expiry > now + 1800:
        return _apns_jwt_token
    
    # Create new token (valid for 1 hour)
    _apns_jwt_expiry = now + 3600
    _apns_jwt_token = jwt.encode(
        {"iss": APNS_TEAM_ID, "iat": now},
        _apns_private_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": APNS_KEY_ID}
    )
    return _apns_jwt_token


def send_apns_notification(device_token: str, payload: dict) -> dict:
    """Send notification via APNs HTTP/2."""
    token = get_apns_jwt()
    if not token:
        return {"success": False, "error": "APNs not configured"}
    
    host = "api.push.apple.com" if APNS_PRODUCTION else "api.sandbox.push.apple.com"
    url = f"https://{host}/3/device/{device_token}"
    
    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + 86400),
        "content-type": "application/json"
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        apns_id = response.headers.get("apns-id")
        
        if response.ok:
            return {"success": True, "apns_id": apns_id}
        
        error_body = response.json() if response.text else {}
        reason = error_body.get("reason", f"HTTP {response.status_code}")
        invalid_token = response.status_code == 410 or reason in ["BadDeviceToken", "Unregistered"]
        
        return {"success": False, "error": reason, "invalid_token": invalid_token, "apns_id": apns_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
# maybe_notify() - COMPLETE NOTIFICATION PIPELINE
# ============================================================

def maybe_notify(result: LotteryResult, analysis: ChrisAnalysis) -> dict:
    """
    Complete notification pipeline:
    1. Determine alert type
    2. Load eligible devices
    3. Respect preferences
    4. Build APNs payload with resultId and analysisId
    5. Send via APNs
    6. Record success/failure
    7. Prevent duplicates
    8. Handle invalid tokens
    """
    notify_result = {
        "qualified": False,
        "alert_type": None,
        "eligible_devices": 0,
        "sent": 0,
        "failed": 0,
        "skipped_duplicate": 0,
        "errors": []
    }
    
    # 1. Determine alert type
    alert_type = None
    for rule in analysis.triggered_rules:
        if rule.rule_type == "QUAD_PRESSURE":
            alert_type = "QUAD_PRESSURE"
            break
        elif rule.rule_type == "TRIPLE_PRESSURE" and alert_type not in ["QUAD_PRESSURE"]:
            alert_type = "TRIPLE_PRESSURE"
        elif rule.rule_type == "DOUBLE_PRESSURE" and alert_type not in ["QUAD_PRESSURE", "TRIPLE_PRESSURE"]:
            alert_type = "DOUBLE_PRESSURE"
        elif rule.rule_type == "PAIR_FOLLOWER" and not alert_type:
            alert_type = "PAIR_FOLLOWER"
    
    if not alert_type:
        return notify_result
    
    notify_result["qualified"] = True
    notify_result["alert_type"] = alert_type
    
    # 2. Load eligible devices
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("""
        SELECT * FROM registered_devices 
        WHERE is_active = 1 AND invalid_token_at IS NULL
    """)
    devices = c.fetchall()
    
    # 3. Filter by preferences
    eligible = []
    for device in devices:
        # Check alert type preference
        if alert_type == "PAIR_FOLLOWER" and not device["notify_pair_follower"]:
            continue
        if alert_type == "DOUBLE_PRESSURE" and not device["notify_double_pressure"]:
            continue
        if alert_type == "TRIPLE_PRESSURE" and not device["notify_triple_pressure"]:
            continue
        if alert_type == "QUAD_PRESSURE" and not device["notify_quad_pressure"]:
            continue
        
        # Check minimum signals
        if analysis.total_signals < device["minimum_signal_count"]:
            continue
        
        # Check state filter
        if device["enabled_states"]:
            states = json.loads(device["enabled_states"])
            if result.state_code not in states and result.state not in states:
                continue
        
        # Check game filter
        if device["enabled_games"]:
            games = json.loads(device["enabled_games"])
            if result.game not in games:
                continue
        
        eligible.append(device)
    
    notify_result["eligible_devices"] = len(eligible)
    
    if not eligible:
        conn.close()
        return notify_result
    
    # 4. Build APNs payload
    emoji = "🔥" if alert_type in ["QUAD_PRESSURE", "TRIPLE_PRESSURE"] else "⚡" if alert_type == "DOUBLE_PRESSURE" else "🧊"
    game_display = "Pick 3" if result.game == "pick3" else "Pick 4"
    
    watch_text = ""
    if analysis.watch_numbers:
        top_watch = [w.number for w in analysis.watch_numbers[:3]]
        watch_text = f"\n\nWATCH: {', '.join(top_watch)}"
    
    payload = {
        "aps": {
            "alert": {
                "title": f"{emoji} ICEBREAKER ALERT",
                "subtitle": f"{result.state} {game_display} {result.draw}",
                "body": f"{result.number} just fell.\n\nChris Pair: {alert_type.replace('_', ' ')}{watch_text}"
            },
            "sound": "default",
            "badge": 1,
            "mutable-content": 1,
            "content-available": 1
        },
        "icebreaker": {
            "type": "chris_analysis",
            "resultId": result.id,
            "analysisId": f"analysis_{result.id}",
            "state": result.state,
            "stateCode": result.state_code,
            "game": result.game,
            "draw": result.draw,
            "number": result.number,  # STRING - preserves "0080"
            "pressureType": analysis.pressure_type,
            "alertType": alert_type
        }
    }
    
    # 5. Send to each device
    for device in eligible:
        device_id = device["id"]
        result_id = result.id
        analysis_id = f"analysis_{result.id}"
        
        # 7. Check for duplicate
        fingerprint = f"{device_id}|{result_id}|{analysis_id}|{alert_type}"
        c.execute("SELECT 1 FROM notification_sent_log WHERE fingerprint = ?", (fingerprint,))
        if c.fetchone():
            notify_result["skipped_duplicate"] += 1
            continue
        
        # Send notification
        send_result = send_apns_notification(device["device_token"], payload)
        
        # 6. Record result
        c.execute("""
            INSERT OR IGNORE INTO notification_sent_log 
            (device_id, result_id, analysis_id, alert_type, fingerprint, sent_at, success, error_message, apns_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            device_id, result_id, analysis_id, alert_type, fingerprint,
            datetime.now().isoformat(),
            1 if send_result["success"] else 0,
            send_result.get("error"),
            send_result.get("apns_id")
        ))
        
        if send_result["success"]:
            notify_result["sent"] += 1
        else:
            notify_result["failed"] += 1
            notify_result["errors"].append(send_result.get("error", "Unknown"))
            
            # 8. Handle invalid token
            if send_result.get("invalid_token"):
                c.execute("""
                    UPDATE registered_devices 
                    SET invalid_token_at = ?, is_active = 0 
                    WHERE id = ?
                """, (datetime.now().isoformat(), device_id))
    
    conn.commit()
    conn.close()
    
    return notify_result


# ============================================================
# RESULT PROCESSOR (Complete Pipeline)
# ============================================================

def process_result(result: LotteryResult) -> dict:
    """
    Process a single result through the complete pipeline:
    SCRAPER → DUPLICATE CHECK → SAVE → CHRIS PAIR → WATCH/PRESSURE → maybe_notify()
    """
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    # Check for duplicate
    c.execute("SELECT 1 FROM lottery_results WHERE fingerprint = ?", (result.fingerprint,))
    if c.fetchone():
        conn.close()
        return {"is_new": False, "duplicate": True}
    
    # Save result
    c.execute("""
        INSERT INTO lottery_results 
        (id, state, state_code, game, draw, number, draw_date, fetched_at, source, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result.id, result.state, result.state_code, result.game,
        result.draw, result.number, result.draw_date, result.fetched_at,
        result.source, result.fingerprint
    ))
    conn.commit()
    
    # Run Chris Pair analysis
    analysis = analyze_number(result.number, result.game)
    
    # Save analysis
    analysis_id = f"analysis_{result.id}"
    c.execute("""
        INSERT INTO chris_analyses
        (id, result_id, source_number, state, state_code, game, draw, draw_date,
         pattern, triggered_rules, watch_numbers, pressure_type, total_signals, predictions, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        analysis_id, result.id, analysis.source_number, result.state, result.state_code,
        result.game, result.draw, result.draw_date, analysis.pattern,
        json.dumps([asdict(r) for r in analysis.triggered_rules]),
        json.dumps([asdict(w) for w in analysis.watch_numbers]),
        analysis.pressure_type, analysis.total_signals,
        json.dumps(analysis.predictions), datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()
    
    # maybe_notify()
    notify_result = maybe_notify(result, analysis)
    
    return {
        "is_new": True,
        "result": asdict(result) if hasattr(result, '__dataclass_fields__') else {
            "id": result.id, "state": result.state, "state_code": result.state_code,
            "game": result.game, "draw": result.draw, "number": result.number,
            "draw_date": result.draw_date
        },
        "analysis": {
            "pattern": analysis.pattern,
            "predictions": analysis.predictions,
            "pressure_type": analysis.pressure_type,
            "total_signals": analysis.total_signals,
            "watch_numbers": [w.number for w in analysis.watch_numbers[:5]]
        },
        "notification": notify_result
    }


# ============================================================
# SCHEDULED SCRAPER
# ============================================================

def scheduled_scrape():
    """Run scheduled scrape of all states."""
    print(f"[{datetime.now()}] Starting scheduled scrape...")
    
    results, errors = fetch_all_states()
    
    new_count = 0
    duplicate_count = 0
    notify_count = 0
    
    for result in results:
        processed = process_result(result)
        if processed.get("is_new"):
            new_count += 1
            if processed.get("notification", {}).get("sent", 0) > 0:
                notify_count += processed["notification"]["sent"]
        else:
            duplicate_count += 1
    
    print(f"[{datetime.now()}] Scrape complete: {new_count} new, {duplicate_count} duplicates, {notify_count} notifications sent")
    if errors:
        print(f"Errors: {errors}")


# ============================================================
# API ENDPOINTS
# ============================================================

# ----- Original Endpoints -----

@app.route("/predict", methods=["GET"])
def predict():
    """
    GET /predict?number=112
    
    Returns Chris Pair predictions for a number.
    Preserves leading zeros.
    """
    number = request.args.get("number", "")
    
    if not number or not number.isdigit():
        return jsonify({"error": "Invalid number"}), 400
    
    if len(number) < 3 or len(number) > 5:
        return jsonify({"error": "Number must be 3-5 digits"}), 400
    
    predictions = run_chris_decoder(number)
    
    return jsonify({
        "number": number,  # Preserves "0080" as "0080"
        "predictions": predictions,
        "count": len(predictions)
    })


@app.route("/card", methods=["GET"])
def card():
    """
    GET /card
    
    Returns the Chris Pair mapping card.
    """
    return jsonify({
        "chris_pairs": CHRIS_PAIRS,
        "description": "Chris Pair bidirectional mappings"
    })


@app.route("/health", methods=["GET"])
def health():
    """
    GET /health
    
    Health check endpoint.
    """
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM registered_devices WHERE is_active = 1")
    device_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notification_sent_log WHERE success = 1")
    notif_count = c.fetchone()[0]
    conn.close()
    
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",
        "apns_enabled": bool(_apns_private_key),
        "registered_devices": device_count,
        "notifications_sent": notif_count
    })


# ----- New API Endpoints -----

@app.route("/api/health", methods=["GET"])
def api_health():
    """GET /api/health - Same as /health for API consistency."""
    return health()


@app.route("/api/chris/analyze", methods=["POST"])
def api_analyze():
    """
    POST /api/chris/analyze
    
    Full Chris Pair analysis of a number.
    Same engine as live results.
    """
    data = request.get_json() or {}
    number = data.get("number", "")
    game = data.get("game")
    
    if not number or not number.isdigit():
        return jsonify({"error": "Invalid number"}), 400
    
    if not game:
        game = "pick3" if len(number) == 3 else "pick4"
    
    analysis = analyze_number(number, game)
    
    return jsonify({
        "analysis": {
            "source_number": analysis.source_number,
            "pattern": analysis.pattern,
            "pressure_type": analysis.pressure_type,
            "total_signals": analysis.total_signals,
            "predictions": analysis.predictions,
            "triggered_rules": [asdict(r) for r in analysis.triggered_rules],
            "watch_numbers": [asdict(w) for w in analysis.watch_numbers]
        }
    })


@app.route("/api/results/latest", methods=["GET"])
def api_results_latest():
    """GET /api/results/latest - Get latest lottery results."""
    limit = min(int(request.args.get("limit", 50)), 200)
    state = request.args.get("state")
    game = request.args.get("game")
    
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    query = "SELECT * FROM lottery_results WHERE 1=1"
    params = []
    
    if state:
        query += " AND (state_code = ? OR state = ?)"
        params.extend([state, state])
    if game:
        query += " AND game = ?"
        params.append(game)
    
    query += " ORDER BY draw_date DESC, fetched_at DESC LIMIT ?"
    params.append(limit)
    
    c.execute(query, params)
    results = [dict(r) for r in c.fetchall()]
    conn.close()
    
    return jsonify({"count": len(results), "results": results})


@app.route("/api/analysis/latest", methods=["GET"])
def api_analysis_latest():
    """GET /api/analysis/latest - Get latest Chris Pair analyses."""
    limit = min(int(request.args.get("limit", 20)), 100)
    
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("""
        SELECT * FROM chris_analyses 
        ORDER BY created_at DESC LIMIT ?
    """, (limit,))
    
    analyses = []
    for row in c.fetchall():
        a = dict(row)
        a["triggered_rules"] = json.loads(a["triggered_rules"])
        a["watch_numbers"] = json.loads(a["watch_numbers"])
        a["predictions"] = json.loads(a["predictions"])
        analyses.append(a)
    
    conn.close()
    return jsonify({"count": len(analyses), "analyses": analyses})


# ----- Device Registration -----

@app.route("/api/devices/register", methods=["POST"])
def api_devices_register():
    """POST /api/devices/register - Register iOS device for push."""
    data = request.get_json() or {}
    device_token = data.get("deviceToken")
    
    if not device_token:
        return jsonify({"error": "deviceToken required"}), 400
    
    platform = data.get("platform", "ios")
    app_version = data.get("appVersion", "1.0.0")
    prefs = data.get("preferences", {})
    
    now = datetime.now().isoformat()
    device_id = hashlib.md5(device_token.encode()).hexdigest()
    
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    # Check if exists
    c.execute("SELECT id FROM registered_devices WHERE device_token = ?", (device_token,))
    existing = c.fetchone()
    
    if existing:
        # Update
        c.execute("""
            UPDATE registered_devices SET
                platform = ?, app_version = ?, updated_at = ?,
                notify_pair_follower = ?, notify_double_pressure = ?,
                notify_triple_pressure = ?, notify_quad_pressure = ?,
                minimum_signal_count = ?, enabled_states = ?, enabled_games = ?,
                is_active = 1, invalid_token_at = NULL
            WHERE device_token = ?
        """, (
            platform, app_version, now,
            1 if prefs.get("notifyPairFollower", True) else 0,
            1 if prefs.get("notifyDoublePressure", True) else 0,
            1 if prefs.get("notifyTriplePressure", True) else 0,
            1 if prefs.get("notifyQuadPressure", True) else 0,
            prefs.get("minimumSignalCount", 1),
            json.dumps(prefs["enabledStates"]) if prefs.get("enabledStates") else None,
            json.dumps(prefs["enabledGames"]) if prefs.get("enabledGames") else None,
            device_token
        ))
        device_id = existing[0]
    else:
        # Insert
        c.execute("""
            INSERT INTO registered_devices
            (id, device_token, platform, app_version, created_at, updated_at,
             notify_pair_follower, notify_double_pressure, notify_triple_pressure, notify_quad_pressure,
             minimum_signal_count, enabled_states, enabled_games)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            device_id, device_token, platform, app_version, now, now,
            1 if prefs.get("notifyPairFollower", True) else 0,
            1 if prefs.get("notifyDoublePressure", True) else 0,
            1 if prefs.get("notifyTriplePressure", True) else 0,
            1 if prefs.get("notifyQuadPressure", True) else 0,
            prefs.get("minimumSignalCount", 1),
            json.dumps(prefs["enabledStates"]) if prefs.get("enabledStates") else None,
            json.dumps(prefs["enabledGames"]) if prefs.get("enabledGames") else None
        ))
    
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "deviceId": device_id}), 201


@app.route("/api/devices/unregister", methods=["DELETE"])
def api_devices_unregister():
    """DELETE /api/devices/unregister - Unregister device."""
    data = request.get_json() or {}
    device_token = data.get("deviceToken")
    
    if not device_token:
        return jsonify({"error": "deviceToken required"}), 400
    
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("UPDATE registered_devices SET is_active = 0 WHERE device_token = ?", (device_token,))
    changed = c.rowcount
    conn.commit()
    conn.close()
    
    if changed:
        return jsonify({"success": True})
    return jsonify({"error": "Device not found"}), 404


@app.route("/api/devices/preferences", methods=["PUT"])
def api_devices_preferences():
    """PUT /api/devices/preferences - Update notification preferences."""
    data = request.get_json() or {}
    device_token = data.get("deviceToken")
    prefs = data.get("preferences", {})
    
    if not device_token:
        return jsonify({"error": "deviceToken required"}), 400
    
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    c.execute("""
        UPDATE registered_devices SET
            notify_pair_follower = ?, notify_double_pressure = ?,
            notify_triple_pressure = ?, notify_quad_pressure = ?,
            minimum_signal_count = ?, updated_at = ?
        WHERE device_token = ?
    """, (
        1 if prefs.get("notifyPairFollower", True) else 0,
        1 if prefs.get("notifyDoublePressure", True) else 0,
        1 if prefs.get("notifyTriplePressure", True) else 0,
        1 if prefs.get("notifyQuadPressure", True) else 0,
        prefs.get("minimumSignalCount", 1),
        datetime.now().isoformat(),
        device_token
    ))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


# ----- Admin Endpoints -----

@app.route("/api/admin/scrape", methods=["POST"])
@require_admin
def api_admin_scrape():
    """POST /api/admin/scrape - Trigger manual scrape."""
    data = request.get_json() or {}
    states = data.get("states")
    
    if states:
        results, errors = fetch_all_states(states)
    else:
        results, errors = fetch_all_states()
    
    processed_results = []
    new_count = 0
    duplicate_count = 0
    
    for result in results:
        processed = process_result(result)
        if processed.get("is_new"):
            new_count += 1
            processed_results.append(processed)
        else:
            duplicate_count += 1
    
    return jsonify({
        "success": True,
        "new_results": new_count,
        "duplicates": duplicate_count,
        "errors": errors,
        "results": processed_results[:10]  # Limit response size
    })


@app.route("/api/admin/test-notification", methods=["POST"])
@require_admin
def api_admin_test_notification():
    """POST /api/admin/test-notification - Send test notification."""
    data = request.get_json() or {}
    device_token = data.get("deviceToken")
    
    if not device_token:
        return jsonify({"error": "deviceToken required"}), 400
    
    if not _apns_private_key:
        return jsonify({"error": "APNs not configured"}), 503
    
    payload = {
        "aps": {
            "alert": {
                "title": "🧊 Icebreaker Test",
                "subtitle": "Connection Verified",
                "body": "APNs notifications are working correctly!"
            },
            "sound": "default",
            "badge": 0
        },
        "icebreaker": {
            "type": "test",
            "resultId": "test",
            "analysisId": "test"
        }
    }
    
    result = send_apns_notification(device_token, payload)
    
    if result["success"]:
        return jsonify({"success": True, "apns_id": result.get("apns_id")})
    return jsonify({"success": False, "error": result.get("error")}), 500


@app.route("/notify/test", methods=["POST"])
def notify_test():
    """
    POST /notify/test
    
    Test APNs connection with a specific device token.
    This is for verifying APNs works before relying on automatic alerts.
    """
    data = request.get_json() or {}
    device_token = data.get("deviceToken") or data.get("device_token")
    
    if not device_token:
        return jsonify({"error": "deviceToken required"}), 400
    
    if not _apns_private_key:
        return jsonify({
            "error": "APNs not configured",
            "hint": "Set APNS_KEY_ID, APNS_TEAM_ID, APNS_KEY_PATH in environment"
        }), 503
    
    payload = {
        "aps": {
            "alert": {
                "title": "🧊 Icebreaker Test",
                "subtitle": "APNs Connection Verified",
                "body": "Your device is ready to receive Chris Pair alerts!"
            },
            "sound": "default",
            "badge": 0,
            "mutable-content": 1
        },
        "icebreaker": {
            "type": "test",
            "result_id": "test",
            "analysis_id": "test",
            "state": "Test",
            "game": "pick4",
            "draw": "Test",
            "number": "0000",
            "alert_type": "TEST"
        }
    }
    
    result = send_apns_notification(device_token, payload)
    
    return jsonify({
        "success": result["success"],
        "apns_id": result.get("apns_id"),
        "error": result.get("error")
    }), 200 if result["success"] else 500


@app.route("/api/admin/devices", methods=["GET"])
@require_admin
def api_admin_devices():
    """GET /api/admin/devices - List registered devices."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM registered_devices WHERE is_active = 1")
    active = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notification_sent_log WHERE success = 1")
    sent = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notification_sent_log WHERE success = 0")
    failed = c.fetchone()[0]
    conn.close()
    
    return jsonify({
        "active_devices": active,
        "notifications_sent": sent,
        "notifications_failed": failed
    })


# ============================================================
# STARTUP
# ============================================================

def start_scheduler():
    """Start background scheduler for periodic scraping."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        scheduled_scrape,
        'interval',
        minutes=SCRAPE_INTERVAL_MINUTES,
        id='scrape_job'
    )
    scheduler.start()
    print(f"Scheduler started: scraping every {SCRAPE_INTERVAL_MINUTES} minutes")


# Initialize on import
init_db()
load_apns_key()

# Start scheduler if not in debug mode
if os.getenv("FLASK_DEBUG") != "1":
    start_scheduler()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
