import os
import time
import json
import threading
import smtplib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from email.message import EmailMessage
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from flask import Flask, jsonify, render_template_string
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from twilio.rest import Client

load_dotenv()

app = Flask(__name__)

BASE_PARKS_URL = os.getenv("PARKS_URL", "")
START_DATE = os.getenv("START_DATE", "2026-06-01")
DAYS_TO_CHECK = int(os.getenv("DAYS_TO_CHECK", "3"))
SERVICE_NAME = os.getenv("SERVICE_NAME", "Shuttle to Lake Louise and Moraine Lake")

EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() == "true"
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "false").lower() == "true"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
WHATSAPP_TO = [x.strip() for x in os.getenv("WHATSAPP_TO", "").split(",") if x.strip()]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

EASTERN_TZ = ZoneInfo("America/Toronto")
STATUS_FILE = Path("/tmp/status.json")
alerted_dates = set()


def eastern_now():
    return datetime.now(EASTERN_TZ)


def now():
    return eastern_now().strftime("%d %b %Y, %I:%M:%S %p %Z")


def now_iso():
    return eastern_now().isoformat()


def generate_check_dates():
    start = datetime.strptime(START_DATE, "%Y-%m-%d")
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS_TO_CHECK)]


CHECK_DATES = generate_check_dates()


HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Parks Canada Availability Bot</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <style>
    body {
      font-family: Arial, sans-serif;
      background: linear-gradient(135deg, #eef2ff, #f8fafc);
      margin: 0;
      padding: 25px;
      color: #0f172a;
    }

    .card {
      background: white;
      border-radius: 24px;
      padding: 28px;
      max-width: 1100px;
      margin: auto;
      box-shadow: 0 15px 35px rgba(15, 23, 42, 0.15);
    }

    h1 {
      margin-top: 0;
      font-size: 30px;
    }

    .subtitle {
      color: #475569;
      margin-bottom: 20px;
    }

    .status {
      font-size: 46px;
      font-weight: bold;
      margin: 20px 0 5px;
    }

    .available { color: #16a34a; }
    .not_available { color: #dc2626; }
    .checking { color: #f59e0b; }
    .starting { color: #2563eb; }
    .error { color: #dc2626; }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 15px;
      margin-top: 20px;
    }

    .box {
      background: #f8fafc;
      padding: 18px;
      border-radius: 16px;
      border: 1px solid #e2e8f0;
    }

    .box h3 {
      margin-top: 0;
    }

    .metric {
      font-size: 14px;
      color: #475569;
    }

    .value {
      font-size: 18px;
      font-weight: bold;
      margin-top: 5px;
    }

    button, a {
      display: inline-block;
      padding: 14px 18px;
      border-radius: 12px;
      background: #1d4ed8;
      color: white;
      text-decoration: none;
      border: 0;
      font-weight: bold;
      margin: 6px 6px 6px 0;
      cursor: pointer;
    }

    button:hover, a:hover {
      opacity: 0.9;
    }

    .secondary {
      background: #64748b;
    }

    .successBtn {
      background: #16a34a;
    }

    .dangerBtn {
      background: #dc2626;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 15px;
      overflow: hidden;
      border-radius: 12px;
    }

    th, td {
      padding: 13px;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
    }

    th {
      background: #eef2ff;
    }

    .yes {
      color: #16a34a;
      font-weight: bold;
    }

    .no {
      color: #dc2626;
      font-weight: bold;
    }

    .manualText {
      color: #f59e0b;
      font-weight: bold;
    }

    .footer-note {
      color: #475569;
      line-height: 1.5;
    }

    .pill {
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      background: #e0f2fe;
      color: #0369a1;
      font-size: 13px;
      font-weight: bold;
    }

    @media(max-width: 600px) {
      body {
        padding: 12px;
      }

      .card {
        padding: 18px;
      }

      .status {
        font-size: 34px;
      }

      table {
        font-size: 13px;
      }

      button, a {
        width: 100%;
        text-align: center;
        box-sizing: border-box;
      }
    }
  </style>
</head>

<body>
  <div class="card">
    <h1>🚐 Parks Canada Availability Dashboard</h1>
    <p class="subtitle">Live checker for Parks Canada shuttle availability</p>

    <p><span class="pill">Eastern Time Enabled</span></p>

    <p>Checking: <b id="service">-</b></p>

    <div id="status" class="status starting">Loading...</div>
    <p id="message">Checking status...</p>

    <div class="grid">
      <div class="box">
        <div class="metric">Start Date</div>
        <div class="value" id="start_date">-</div>
      </div>

      <div class="box">
        <div class="metric">Days Checked</div>
        <div class="value" id="days_to_check">-</div>
      </div>

      <div class="box">
        <div class="metric">Available Dates</div>
        <div class="value" id="available_dates">-</div>
      </div>

      <div class="box">
        <div class="metric">Last Checked</div>
        <div class="value" id="last_checked">-</div>
      </div>

      <div class="box">
        <div class="metric">Check Interval</div>
        <div class="value" id="interval">-</div>
      </div>

      <div class="box">
        <div class="metric">Next Auto Check</div>
        <div class="value" id="next_check">Calculating...</div>
      </div>

      <div class="box">
        <div class="metric">Email</div>
        <div class="value" id="email">-</div>
      </div>

      <div class="box">
        <div class="metric">WhatsApp</div>
        <div class="value" id="whatsapp">-</div>
      </div>

      <div class="box">
        <div class="metric">Telegram</div>
        <div class="value" id="telegram">-</div>
      </div>
    </div>

    <br>

    <button onclick="checkNow()">Check Now</button>
    <button class="secondary" onclick="testEmail()">Test Email</button>
    <button class="secondary" onclick="testWhatsapp()">Test WhatsApp</button>
    <button class="secondary" onclick="testTelegram()">Test Telegram</button>
    <a id="parksLink" href="#" target="_blank">Open Parks Canada</a>

    <div class="box">
      <h3>Date-wise Availability</h3>
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Status</th>
            <th>Message</th>
          </tr>
        </thead>
        <tbody id="date_table">
          <tr><td colspan="3">No check completed yet.</td></tr>
        </tbody>
      </table>
    </div>

    <div class="box" style="margin-top:15px;">
      <h3>Important</h3>
      <p class="footer-note">
        This bot only checks availability and sends alerts. It does not bypass CAPTCHA, queue, login, booking, or payment.
      </p>
    </div>
  </div>

<script>
let lastCheckedIso = null;
let intervalSeconds = 300;

async function loadStatus() {
  const res = await fetch('/api/status');
  const data = await res.json();

  const status = document.getElementById('status');
  status.innerText = data.status_label;
  status.className = 'status ' + data.status;

  document.getElementById('message').innerText = data.message;
  document.getElementById('service').innerText = data.service_name;
  document.getElementById('start_date').innerText = data.start_date;
  document.getElementById('days_to_check').innerText = data.days_to_check;

  document.getElementById('available_dates').innerText =
    data.available_dates && data.available_dates.length ? data.available_dates.join(', ') : 'None';

  document.getElementById('last_checked').innerText = data.last_checked || 'Not checked yet';
  document.getElementById('interval').innerText = data.check_interval_seconds + ' seconds';

  document.getElementById('email').innerText = data.email_configured ? 'Configured' : 'Missing';
  document.getElementById('whatsapp').innerText = data.whatsapp_configured ? 'Configured' : 'Missing';
  document.getElementById('telegram').innerText = data.telegram_configured ? 'Configured' : 'Missing';

  document.getElementById('parksLink').href = data.parks_url || '#';

  lastCheckedIso = data.last_checked_iso;
  intervalSeconds = data.check_interval_seconds || 300;

  const table = document.getElementById('date_table');
  table.innerHTML = '';

  if (!data.date_results || data.date_results.length === 0) {
    table.innerHTML = '<tr><td colspan="3">No check completed yet.</td></tr>';
  } else {
    data.date_results.forEach(row => {
      let cls = row.status === 'Available' ? 'yes' : row.status === 'Manual Check' ? 'manualText' : 'no';
      table.innerHTML += `
        <tr>
          <td>${row.date}</td>
          <td class="${cls}">${row.status}</td>
          <td>${row.message}</td>
        </tr>
      `;
    });
  }
}

function updateCountdown() {
  const el = document.getElementById('next_check');

  if (!lastCheckedIso) {
    el.innerText = 'Waiting for first check';
    return;
  }

  const last = new Date(lastCheckedIso);
  const next = new Date(last.getTime() + intervalSeconds * 1000);
  const now = new Date();

  let diff = Math.floor((next - now) / 1000);

  if (diff <= 0) {
    el.innerText = 'Any moment now...';
    return;
  }

  const mins = Math.floor(diff / 60);
  const secs = diff % 60;

  el.innerText = `${mins} min ${secs} sec`;
}

async function checkNow() {
  await fetch('/api/check-now', { method: 'POST' });
  alert('Manual check started.');
  setTimeout(loadStatus, 5000);
}

async function testEmail() {
  const res = await fetch('/api/test-email', { method: 'POST' });
  const data = await res.json();
  alert(data.message);
}

async function testWhatsapp() {
  const res = await fetch('/api/test-whatsapp', { method: 'POST' });
  const data = await res.json();
  alert(data.message);
}

async function testTelegram() {
  const res = await fetch('/api/test-telegram', { method: 'POST' });
  const data = await res.json();
  alert(data.message);
}

loadStatus();
setInterval(loadStatus, 10000);
setInterval(updateCountdown, 1000);
</script>
</body>
</html>
"""


def default_status():
    return {
        "status": "starting",
        "status_label": "Starting",
        "message": "Bot is starting...",
        "last_checked": None,
        "last_checked_iso": None,
        "parks_url": BASE_PARKS_URL,
        "service_name": SERVICE_NAME,
        "start_date": START_DATE,
        "days_to_check": DAYS_TO_CHECK,
        "check_dates": CHECK_DATES,
        "available_dates": [],
        "date_results": [],
        "check_interval_seconds": CHECK_INTERVAL_SECONDS,
        "email_configured": bool(EMAIL_FROM and EMAIL_TO and EMAIL_PASSWORD),
        "whatsapp_configured": bool(
            WHATSAPP_ENABLED and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM and WHATSAPP_TO
        ),
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }


def read_status():
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            return default_status()
    return default_status()


def write_status(data):
    current = read_status()
    current.update(data)
    STATUS_FILE.write_text(json.dumps(current, indent=2))


def build_date_url(check_date):
    parsed = urlparse(BASE_PARKS_URL)
    query = parse_qs(parsed.query)

    start = datetime.strptime(check_date, "%Y-%m-%d")
    end = start + timedelta(days=1)

    query["startDate"] = [check_date]
    query["endDate"] = [end.strftime("%Y-%m-%d")]
    query["nights"] = ["1"]
    query["isReserving"] = ["true"]
    query["view"] = ["list"]

    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def send_email_alert(subject, body):
    if not EMAIL_ENABLED:
        raise Exception("EMAIL_ENABLED is false.")

    if not EMAIL_FROM:
        raise Exception("EMAIL_FROM is missing.")

    if not EMAIL_TO:
        raise Exception("EMAIL_TO is missing.")

    if not EMAIL_PASSWORD:
        raise Exception("EMAIL_PASSWORD is missing.")

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg["Subject"] = subject
    msg.set_content(body)

    clean_password = EMAIL_PASSWORD.replace(" ", "").strip()

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.login(EMAIL_FROM, clean_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(EMAIL_FROM, clean_password)
                server.send_message(msg)

        return True

    except Exception as e:
        raise Exception(f"Email failed: {str(e)}")


def send_whatsapp(message):
    if not WHATSAPP_ENABLED:
        return False

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM or not WHATSAPP_TO:
        raise Exception("WhatsApp/Twilio is not configured.")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    for number in WHATSAPP_TO:
        client.messages.create(
            body=message,
            from_=TWILIO_FROM,
            to=number
        )

    return True


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    return True


def analyze_text(text):
    text = text.lower()

    if "captcha" in text or "recaptcha" in text:
        return None, "CAPTCHA detected."

    if "queue" in text or "waiting room" in text:
        return None, "Queue detected."

    available_words = [
        "available",
        "reserve",
        "book now",
        "disponible",
        "réserver",
    ]

    not_available_words = [
        "no availability",
        "not available",
        "aucune disponibilité",
        "aucun résultat",
        "sold out",
        "unavailable",
    ]

    has_available = any(word in text for word in available_words)
    has_not_available = any(word in text for word in not_available_words)

    if has_available and not has_not_available:
        return True, "Possible availability found."

    if has_available and has_not_available:
        return None, "Mixed result. Please manually verify."

    return False, "No availability found."


def check_single_date(check_date):
    url = build_date_url(check_date)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        text = page.inner_text("body")
        browser.close()

        result, message = analyze_text(text)
        return result, message, url


def perform_check():
    available_dates = []
    date_results = []

    write_status({
        "status": "checking",
        "status_label": "Checking",
        "message": f"Checking {SERVICE_NAME} from {START_DATE} for {DAYS_TO_CHECK} days...",
        "last_checked": now(),
        "last_checked_iso": now_iso(),
    })

    try:
        if not BASE_PARKS_URL:
            raise Exception("PARKS_URL is missing in Railway variables.")

        for check_date in CHECK_DATES:
            result, message, url = check_single_date(check_date)

            if result is True:
                available_dates.append(check_date)
                date_results.append({
                    "date": check_date,
                    "status": "Available",
                    "message": message
                })

            elif result is None:
                date_results.append({
                    "date": check_date,
                    "status": "Manual Check",
                    "message": message
                })

            else:
                date_results.append({
                    "date": check_date,
                    "status": "Not Available",
                    "message": message
                })

            write_status({
                "status": "checking",
                "status_label": "Checking",
                "message": f"Checked {check_date}. Continuing...",
                "date_results": date_results,
                "available_dates": available_dates,
                "last_checked": now(),
                "last_checked_iso": now_iso(),
            })

        if available_dates:
            available_text = ", ".join(available_dates)

            write_status({
                "status": "available",
                "status_label": "Available",
                "message": f"Available dates found: {available_text}",
                "available_dates": available_dates,
                "date_results": date_results,
                "last_checked": now(),
                "last_checked_iso": now_iso(),
            })

            new_dates = [d for d in available_dates if d not in alerted_dates]

            if new_dates:
                new_text = ", ".join(new_dates)

                alert_message = f"""
Parks Canada availability found!

Service:
{SERVICE_NAME}

Available date(s):
{new_text}

Book manually:
{BASE_PARKS_URL}

Checked at:
{now()}
"""

                send_email_alert(
                    subject=f"Parks Canada Availability Found: {new_text}",
                    body=alert_message
                )

                send_whatsapp(alert_message)
                send_telegram(alert_message)

                for d in new_dates:
                    alerted_dates.add(d)

        else:
            write_status({
                "status": "not_available",
                "status_label": "Not Available",
                "message": "No availability found for the checked dates.",
                "available_dates": [],
                "date_results": date_results,
                "last_checked": now(),
                "last_checked_iso": now_iso(),
            })

    except Exception as e:
        write_status({
            "status": "error",
            "status_label": "Error",
            "message": str(e),
            "last_checked": now(),
            "last_checked_iso": now_iso(),
        })


def bot_loop():
    while True:
        perform_check()
        time.sleep(CHECK_INTERVAL_SECONDS)


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    return jsonify(read_status())


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    threading.Thread(target=perform_check, daemon=True).start()
    return jsonify({"ok": True, "message": "Check started"})


@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    try:
        send_email_alert(
            "Test Email from Parks Canada Bot",
            f"This is a test email from your Parks Canada availability dashboard.\\n\\nChecked at: {now()}"
        )
        return jsonify({"ok": True, "message": "Test email sent."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/test-email-direct")
def api_test_email_direct():
    try:
        send_email_alert(
            "Direct Test Email from Parks Canada Bot",
            f"""
Hello,

This is a direct browser-triggered email test.

If you received this email, Gmail SMTP is working.

Checked at:
{now()}
"""
        )
        return jsonify({"ok": True, "message": "Direct test email sent."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/test-whatsapp", methods=["POST"])
def api_test_whatsapp():
    try:
        send_whatsapp(f"Test WhatsApp from Parks Canada Availability Bot. Checked at: {now()}")
        return jsonify({"ok": True, "message": "Test WhatsApp sent."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    try:
        send_telegram(f"Test alert from Parks Canada Dashboard Bot. Checked at: {now()}")
        return jsonify({"ok": True, "message": "Telegram test sent."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
