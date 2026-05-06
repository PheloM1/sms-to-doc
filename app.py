"""
SMS → AI → Google Doc Pipeline
BME 14:125:498 Assignment
"""

import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_DOC_ID       = os.environ["GOOGLE_DOC_ID"]          # Share this doc with your service account email
SERVICE_ACCOUNT_JSON = os.environ["SERVICE_ACCOUNT_JSON"]  # Full JSON string of service account key

DB_PATH = "messages.db"

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            sender    TEXT    NOT NULL,
            body      TEXT    NOT NULL,
            received  TEXT    NOT NULL
        )
    """)
    con.commit()
    con.close()

def save_message(sender: str, body: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO messages (sender, body, received) VALUES (?, ?, ?)",
        (sender, body, datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()

def get_all_messages() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT sender, body, received FROM messages ORDER BY received"
    ).fetchall()
    con.close()
    return [{"sender": r[0], "body": r[1], "received": r[2]} for r in rows]

# ── Twilio webhook ─────────────────────────────────────────────────────────────
@app.route("/sms", methods=["POST"])
def sms_webhook():
    # Validate the request is genuinely from Twilio
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    url       = request.url
    params    = request.form.to_dict()
    signature = request.headers.get("X-Twilio-Signature", "")

    if not validator.validate(url, params, signature):
        return Response("Forbidden", status=403)

    sender = request.form.get("From", "unknown")
    body   = request.form.get("Body", "").strip()

    if body:
        save_message(sender, body)
        update_google_doc()

    resp = MessagingResponse()
    resp.message("✅ Received! Your message has been added to the document.")
    return str(resp), 200, {"Content-Type": "text/xml"}

# ── AI processing ─────────────────────────────────────────────────────────────
def build_document_with_claude(messages: list[dict]) -> str:
    """Send all messages to Claude and get back a polished structured document."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    formatted = "\n".join(
        f"[{m['received']} | {m['sender']}]: {m['body']}"
        for m in messages
    )

    system = (
        "You are a document editor. You receive raw SMS messages from one or more contributors "
        "and transform them into a coherent, well-organized document. "
        "Group related ideas, remove redundancy, fix grammar, and add helpful headings. "
        "Return ONLY the document text — no preamble, no markdown fences."
    )

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"Here are the SMS messages received so far:\n\n{formatted}\n\n"
                "Please transform these into a polished, structured document."
            )
        }]
    )

    return response.content[0].text

# ── Google Docs writer ────────────────────────────────────────────────────────
def get_docs_service():
    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/documents"]
    )
    return build("docs", "v1", credentials=creds)

def update_google_doc():
    messages = get_all_messages()
    if not messages:
        return

    new_content = build_document_with_claude(messages)
    service     = get_docs_service()

    # Get current doc length so we can clear it first
    doc    = service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    body   = doc.get("body", {})
    end_ix = body.get("content", [{}])[-1].get("endIndex", 1)

    requests_payload = []

    # Delete existing content (keep 1 char — Docs requires at least 1)
    if end_ix > 2:
        requests_payload.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_ix - 1}
            }
        })

    # Insert updated content
    requests_payload.append({
        "insertText": {
            "location": {"index": 1},
            "text": new_content
        }
    })

    service.documents().batchUpdate(
        documentId=GOOGLE_DOC_ID,
        body={"requests": requests_payload}
    ).execute()

    print(f"[{datetime.utcnow().isoformat()}] Google Doc updated ✓")

# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok", "messages": len(get_all_messages())}, 200

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
