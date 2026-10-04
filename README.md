# MailFlow — Email Campaign Manager

MailFlow is a lightweight email campaign manager built with **Python + FastAPI + Jinja2 + SMTP**. It is based on the original FlaskPost idea, redesigned with a safer architecture and a modern dashboard.

## Features

- SMTP configuration with STARTTLS / SSL / none
- SMTP connection test
- CSV upload and validation
- Duplicate detection
- CSV preview
- Jinja personalization such as `{{ name }}` and `{{ company }}`
- Live HTML email preview
- Background campaign sending
- Retry handling
- Configurable sending interval
- Live campaign progress
- Stop campaign request
- Recent campaign history for the current process
- SMTP credentials are never written to logs

## Project structure

```text
MailFlow/
├── app.py
├── requirements.txt
├── .env.example
├── Frontend/
│   └── index.html
├── assets/
│   └── favicons...
└── README.md
```

## Run locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8000`.

## CSV example

```csv
Email,name,company
ahmed@example.com,Ahmed,ABC Company
sara@example.com,Sara,XYZ Company
```

Use the values in the HTML template:

```html
<h1>Hello {{ name }}</h1>
<p>Welcome to {{ company }}.</p>
```

## SMTP examples

### Gmail / Google Workspace

- Host: `smtp.gmail.com`
- Port: `587`
- Security: `STARTTLS`

Use an **App Password** where required; do not use or publish your normal account password.

### Outlook / Microsoft 365

Use the SMTP settings enabled for the account/tenant. Authentication requirements can vary by organization.

## Important architecture note

Campaigns and SMTP credentials are currently kept **in memory**. This is intentionally simple for the first version and is suitable for local development/testing. A production deployment should move campaign state to a database/queue and credentials to a secure secret store.

## Recommended next upgrades

1. Persistent database (SQLite/PostgreSQL)
2. Redis/Celery or another durable job queue
3. Multiple user accounts and authentication
4. Saved templates and contact lists
5. Attachments
6. Scheduling
7. Unsubscribe management and suppression lists
8. Delivery/bounce tracking via a transactional email provider
9. Production secret management
10. Provider-specific sending limits and compliance controls
# MailFlow
