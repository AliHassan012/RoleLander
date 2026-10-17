# RoleLander

An AI-assisted job and internship research workspace. RoleLander confirms a candidate profile, applies user-selected career and location preferences, researches public vacancies, explains the evidence, and pauses for human decisions before changing search requirements.

## Current implementation

- Python 3.12, FastAPI, SQLite, and plain HTML/CSS/JavaScript.
- Google sign-in; editable and explicitly confirmed candidate profiles; a bundled fictional sample.
- Gemini/Tavily adapters and a bounded research controller with persistent human decisions.
- Country, applicant eligibility, city/radius, work-arrangement, company and disclosed-pay filtering.
- Source-backed matching, ranking and role-fit refresh after approved target-role changes.
- Manual company approval/blocking and bounded Greenhouse/Lever board readers.
- Editable application email templates, reviewed versions, downloads and a manual tracker.

**Status:** 64 regression tests passed using synthetic fixtures and mocked providers. Live provider requests, Google OAuth and public hosting have not been verified. Automatic company recommendations/feed integration, currency conversion, Gmail, scheduled checks and notifications are unfinished. See [build status](docs/BUILD-STATUS.md).

## Run locally

```powershell
git clone https://github.com/AliHassan012/RoleLander.git
cd RoleLander
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
```

Edit the private `.env` with your own configuration, then run:

```powershell
.venv\Scripts\python -m app.start
```

Open http://127.0.0.1:8000. Basic Google OAuth credentials and the exact callback URL are required to sign in. Gemini and Tavily keys are required for live research; missing keys do not prevent `/healthz` from starting. The app loads local `.env` values without overriding existing host environment variables.

Keep `PERSONAL_DATA_MODE=synthetic` for the fictional sample. The sample still requires explicit human confirmation. Real-person profile processing stays blocked until suitable provider data handling has been verified; do not enable `verified_provider` merely to bypass this restriction. Use free access first and verify provider billing settings independently.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

Provider tests use fixtures and `httpx.MockTransport`; they make no live or paid requests.

## Deployment

The included Dockerfile serves the frontend and Python backend from one process on `0.0.0.0`, using the host's `PORT` (default 8000). See [deployment instructions](docs/DEPLOYMENT.md).

GitHub hosts this source repository. GitHub Pages cannot run this Python backend. The full application needs a backend/container host; the event's intended hosting target is Alternate Clouds.

Never commit `.env`, provider credentials, OAuth tokens, databases or personal CV/inbox data. There is no automatic application submission or recruiter-email sending.
