# RoleLander build status

Published source snapshot: 17 October 2026.
Repository: https://github.com/AliHassan012/RoleLander

## Verified locally

- 70 regression tests passed in 11.38 seconds in an isolated copy of this source using the project's Python environment.
- Synthetic sample -> human confirmation -> preferences -> simulated search/extraction -> ranked evidence -> approved preference change -> reassessed results -> editable application draft.
- Target-role changes refresh only role fit from saved public evidence; unchanged target roles reuse findings. Quota, invalid-response, time-limit and budget failures keep the new role fit unknown with zero role points.
- Refresh requests consume the existing AI-attempt budget. Search/extraction counts do not increase. Cancellation and restart recovery are covered.
- Extraction allows at most two structured calls across schema and evidence repair. A persistently invalid listing is recorded as rejected while the run continues to other sources. Regression coverage includes fabricated quotes, corrected evidence, schema repair, quota propagation and a bad listing followed by a valid listing through the complete workflow.
- The regression suite uses fixtures and httpx.MockTransport; it makes no live provider calls.
- Separate live checks succeeded for Gemini structured output, Tavily search and local Google sign-in. A real research run reached live search and page extraction; its evidence rejection exposed the failure handling fixed above.

## Unverified or unfinished

- Complete live research-to-draft flow after the extraction fix, hosted Google OAuth and teammate sign-in.
- Alternate Clouds account, public URL, persistent volume and always-running service.
- Automatic company recommendations and company-feed matching integration.
- Foreign-currency compensation conversion.
- Gmail consent, account/token lifecycle and public-link discovery.
- Durable daily/weekly schedules and in-app/self-email notifications.
- Hosted restart recovery, recording and event submission.

This is a partial implementation. Repository publication is not a live application deployment. Real personal-data AI processing remains gated; the default demonstration uses the bundled fictional sample.
