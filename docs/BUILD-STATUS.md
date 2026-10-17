# RoleLander build status

Published source snapshot: 17 October 2026.
Repository: https://github.com/AliHassan012/RoleLander

## Verified locally

- 64 regression tests passed in 9.51 seconds in an isolated copy of this source using the project's Python environment.
- Synthetic sample -> human confirmation -> preferences -> simulated search/extraction -> ranked evidence -> approved preference change -> reassessed results -> editable application draft.
- Target-role changes refresh only role fit from saved public evidence; unchanged target roles reuse findings. Quota, invalid-response, time-limit and budget failures keep the new role fit unknown with zero role points.
- Refresh requests consume the existing AI-attempt budget. Search/extraction counts do not increase. Cancellation and restart recovery are covered.
- Fixtures and httpx.MockTransport were used. No live or paid API call is claimed.

## Unverified or unfinished

- Actual Gemini/Tavily access and free-tier account/billing status.
- Google OAuth/test-user setup and authenticated browser workflow.
- Alternate Clouds account, public URL, persistent volume and always-running service.
- Automatic company recommendations and company-feed matching integration.
- Foreign-currency compensation conversion.
- Gmail consent, account/token lifecycle and public-link discovery.
- Durable daily/weekly schedules and in-app/self-email notifications.
- Hosted restart recovery, recording and event submission.

This is a partial implementation. Repository publication is not a live application deployment. Real personal-data AI processing remains gated; the default demonstration uses the bundled fictional sample.
