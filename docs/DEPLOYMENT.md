# Deploying RoleLander

## Hosting requirement

Use a host that can run the included Python container, make outbound HTTPS requests, retain a writable SQLite data volume, and keep the service running. The competition's intended host is Alternate Clouds. Confirm the actual account supports these capabilities before claiming reliable monitoring. Publishing this repository does not start a server, and GitHub Pages cannot serve the backend.

## Container

Build from the repository root with the included Dockerfile. For a local Docker check:

```text
docker build -t rolelander .
docker run --rm -p 8000:8000 --env-file .env -v rolelander-data:/app/data rolelander
```

The service binds to 0.0.0.0, respects PORT, uses one Uvicorn worker, and stores SQLite under DATA_DIR. The image includes application code and the fictional sample; it excludes private configuration and databases.

## Host configuration

Set these values privately in the hosting service's environment/secret controls:

| Variable | Value |
| --- | --- |
| GEMINI_API_KEY | Your privately entered key; verify free-tier billing first |
| GEMINI_MODEL | A model actually available to the configured account |
| TAVILY_API_KEY | Your privately entered free-plan key |
| GOOGLE_CLIENT_ID | Google web OAuth client ID |
| GOOGLE_CLIENT_SECRET | Google OAuth secret |
| GOOGLE_REDIRECT_URI | https://YOUR-HOST/auth/google/callback |
| GOOGLE_GMAIL_REDIRECT_URI | https://YOUR-HOST/auth/google/gmail/callback; Gmail is not implemented yet |
| APP_ORIGIN | https://YOUR-HOST, without a trailing slash |
| COOKIE_SECURE | true for public HTTPS |
| FREE_ONLY | true; also verify provider-side billing configuration |
| PERSONAL_DATA_MODE | synthetic |
| DATA_DIR | /app/data, mounted on persistent storage |
| STORAGE_PERSISTENT | true only after storage/restart verification |
| PORT | Host-assigned value, or 8000 if configurable |

Do not place secrets in repository files or browser code. Configure the exact Google OAuth callback, consent screen and allowed test users. The sample is explicitly fictional and requires confirmation. Ordinary personal profiles remain blocked in synthetic mode.

## Release checks

1. GET /healthz returns 200 over public HTTPS and static assets load.
2. Google sign-in succeeds, cookie/CSRF checks work, and a different account cannot access private records.
3. The fictional sample can be loaded and confirmed; preferences are explicit.
4. With actual free-tier credentials, a public vacancy is retrieved and evidence/ranking display correctly.
5. A human decision persists and changes the resumed assessment; an editable draft can be saved.
6. Restart the host and verify the SQLite state survives on the mounted volume.

Record actual URLs, build identifiers, results and limitations. Do not claim Gmail, salary FX, automatic company recommendations or schedules until implemented and verified. SCHEDULER_ENABLED currently does not establish that a scheduler exists.

The recorded competition freeze is 17 October 2026 at 12:45 PM Asia/Karachi unless organizers announce a revision. Complete deployment and required submission evidence before the actual deadline.
