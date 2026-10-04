import asyncio
import csv
import io
import logging
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template, TemplateError

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "Frontend"
ASSETS_DIR = BASE_DIR / "assets"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mailflow")

app = FastAPI(title="MailFlow", version="1.0.0")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_CSV_BYTES = 5 * 1024 * 1024
DEFAULT_INTERVAL = 1.0
RETRY_ATTEMPTS = 3

smtp_config: dict[str, Any] = {}
campaigns: dict[str, dict[str, Any]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email.strip()))


def decode_csv(content: bytes) -> str:
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV file is too large (maximum 5 MB).")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc


def parse_csv(content: bytes) -> tuple[list[dict[str, str]], list[str], int]:
    text = decode_csv(content)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV is empty or has no header row.")

    fieldnames = [str(field).strip() for field in reader.fieldnames if field]
    email_column = next((f for f in fieldnames if f.lower() == "email"), None)
    if not email_column:
        raise HTTPException(status_code=400, detail="CSV must contain an Email column.")

    rows: list[dict[str, str]] = []
    invalid: list[str] = []
    seen: set[str] = set()
    duplicates = 0

    for raw_row in reader:
        row = {str(k).strip(): (v or "").strip() for k, v in raw_row.items() if k}
        email = row.get(email_column, "").strip()
        if not email or not is_valid_email(email):
            if email:
                invalid.append(email)
            continue
        normalized = email.lower()
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        row["Email"] = email
        rows.append(row)

    return rows, invalid, duplicates


def render_template(source: str, row: dict[str, str]) -> str:
    try:
        return Template(source, autoescape=False).render(**row)
    except TemplateError as exc:
        raise ValueError(f"Template error: {exc}") from exc


def smtp_connection(config: dict[str, Any] | None = None) -> smtplib.SMTP:
    config = config or smtp_config
    if not config:
        raise RuntimeError("SMTP is not configured")

    host = config["host"]
    port = int(config["port"])
    username = config["username"]
    password = config["password"]
    security = config["security"]
    timeout = 20

    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        if security == "tls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()

    server.login(username, password)
    return server


def send_one(server: smtplib.SMTP, recipient: str, subject: str, html: str, sender_name: str, sender_email: str) -> None:
    message = EmailMessage()
    message["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("This email requires an HTML-capable email client.")
    message.add_alternative(html, subtype="html")
    server.send_message(message)


async def send_campaign(campaign_id: str, rows: list[dict[str, str]], subject: str, sender_name: str, html_content: str, interval: float, config: dict[str, Any]) -> None:
    campaign = campaigns[campaign_id]
    campaign["status"] = "sending"
    server: smtplib.SMTP | None = None

    try:
        # Use the SMTP snapshot captured when the campaign was created.
        server = await asyncio.to_thread(smtp_connection, config)
        for index, row in enumerate(rows):
            if campaign.get("stop_requested"):
                campaign["status"] = "stopped"
                break
            recipient = row["Email"]
            try:
                personalized_subject = render_template(subject, row)
                personalized_html = render_template(html_content, row)
                last_error = ""

                for attempt in range(1, RETRY_ATTEMPTS + 1):
                    try:
                        await asyncio.to_thread(
                            send_one,
                            server,
                            recipient,
                            personalized_subject,
                            personalized_html,
                            sender_name,
                            config["username"],
                        )
                        campaign["sent"] += 1
                        break
                    except Exception as exc:  # SMTP providers can raise different exception types.
                        last_error = str(exc)
                        if attempt < RETRY_ATTEMPTS:
                            await asyncio.sleep(min(interval * attempt, 10))
                else:
                    campaign["failed"] += 1
                    campaign["errors"].append({"email": recipient, "error": last_error})

            except Exception as exc:
                campaign["failed"] += 1
                campaign["errors"].append({"email": recipient, "error": str(exc)})

            campaign["processed"] = index + 1
            campaign["updated_at"] = now_iso()
            if index < len(rows) - 1:
                await asyncio.sleep(max(0, interval))

        if campaign.get("status") != "stopped":
            campaign["status"] = "completed"
        logger.info("Campaign %s finished: status=%s sent=%s failed=%s", campaign_id, campaign["status"], campaign["sent"], campaign["failed"])
    except Exception as exc:
        campaign["status"] = "failed"
        campaign["fatal_error"] = str(exc)
        logger.exception("Campaign %s failed", campaign_id)
    finally:
        if server:
            try:
                await asyncio.to_thread(server.quit)
            except Exception:
                pass
        campaign["updated_at"] = now_iso()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "MailFlow"}


@app.get("/api/smtp/status")
async def smtp_status() -> dict[str, Any]:
    return {
        "configured": bool(smtp_config),
        "host": smtp_config.get("host", ""),
        "port": smtp_config.get("port", ""),
        "username": smtp_config.get("username", ""),
        "security": smtp_config.get("security", "tls"),
    }


@app.post("/api/smtp/configure")
async def configure_smtp(
    smtpHost: str = Form(...),
    smtpPort: int = Form(...),
    smtpUser: str = Form(...),
    smtpPass: str = Form(...),
    smtpSecurity: str = Form("tls"),
) -> JSONResponse:
    if smtpSecurity not in {"none", "tls", "ssl"}:
        raise HTTPException(status_code=400, detail="Security must be none, tls, or ssl.")
    if not smtpHost.strip() or not smtpUser.strip() or not smtpPass:
        raise HTTPException(status_code=400, detail="All SMTP fields are required.")
    if not 1 <= smtpPort <= 65535:
        raise HTTPException(status_code=400, detail="Invalid SMTP port.")

    smtp_config.clear()
    smtp_config.update({
        "host": smtpHost.strip(),
        "port": smtpPort,
        "username": smtpUser.strip(),
        "password": smtpPass,
        "security": smtpSecurity,
    })
    logger.info("SMTP configuration updated for host=%s port=%s security=%s", smtpHost.strip(), smtpPort, smtpSecurity)
    return JSONResponse({"success": True, "message": "SMTP configuration saved for this session."})


@app.post("/api/smtp/test")
async def test_smtp() -> JSONResponse:
    if not smtp_config:
        raise HTTPException(status_code=400, detail="Configure SMTP first.")
    server = None
    try:
        server = await asyncio.to_thread(smtp_connection)
        return JSONResponse({"success": True, "message": "SMTP connection successful."})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SMTP connection failed: {exc}") from exc
    finally:
        if server:
            try:
                await asyncio.to_thread(server.quit)
            except Exception:
                pass


@app.post("/api/csv/preview")
async def preview_csv(csvFile: UploadFile = File(...)) -> JSONResponse:
    if not csvFile.filename or not csvFile.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file.")
    content = await csvFile.read()
    rows, invalid, duplicates = parse_csv(content)
    columns = list(rows[0].keys()) if rows else []
    return JSONResponse({
        "columns": columns,
        "preview": rows[:8],
        "total": len(rows),
        "invalid": len(invalid),
        "duplicates": duplicates,
        "invalid_emails": invalid[:20],
    })


@app.post("/api/campaigns")
async def create_campaign(
    subject: str = Form(...),
    senderName: str = Form(...),
    htmlContent: str = Form(...),
    csvFile: UploadFile = File(...),
    interval: float = Form(DEFAULT_INTERVAL),
) -> JSONResponse:
    if not smtp_config:
        raise HTTPException(status_code=400, detail="Configure SMTP before creating a campaign.")
    if not subject.strip() or not senderName.strip() or not htmlContent.strip():
        raise HTTPException(status_code=400, detail="Subject, sender name and HTML content are required.")
    if interval < 0 or interval > 60:
        raise HTTPException(status_code=400, detail="Interval must be between 0 and 60 seconds.")

    content = await csvFile.read()
    rows, invalid, duplicates = parse_csv(content)
    if not rows:
        raise HTTPException(status_code=400, detail="No valid recipients were found in the CSV.")

    # Validate Jinja templates before starting a campaign.
    try:
        Template(subject)
        Template(htmlContent)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=f"Template error: {exc}") from exc

    campaign_id = uuid.uuid4().hex[:10]
    campaigns[campaign_id] = {
        "id": campaign_id,
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "total": len(rows),
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "invalid": len(invalid),
        "duplicates": duplicates,
        "errors": [],
        "fatal_error": None,
        "subject": subject.strip(),
    }

    config_snapshot = smtp_config.copy()
    asyncio.create_task(send_campaign(campaign_id, rows, subject.strip(), senderName.strip(), htmlContent, interval, config_snapshot))
    return JSONResponse({"success": True, "campaign": campaigns[campaign_id]})


@app.get("/api/campaigns")
async def list_campaigns() -> JSONResponse:
    ordered = sorted(campaigns.values(), key=lambda item: item["created_at"], reverse=True)
    return JSONResponse({"campaigns": ordered[:25]})


@app.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str) -> JSONResponse:
    campaign = campaigns.get(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return JSONResponse(campaign)


@app.post("/api/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str) -> JSONResponse:
    campaign = campaigns.get(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    # Cooperative stop flag; the sender checks it between recipients.
    campaign["stop_requested"] = True
    campaign["status"] = "stopping"
    return JSONResponse({"success": True, "message": "Stop requested."})


@app.get("/vercel")
async def vercel() -> dict[str, str]:
    return {"message": "MailFlow is running on Vercel!"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
