# PARTYWIN staging bootstrap

This branch contains the complete PARTYWIN Stage 8 staging build packaged as `partywin-stage8-staging-qa.zip` plus a root Dockerfile that extracts and runs it during deployment.

The archive contains the application, tests, CI config, QA checklist, PostgreSQL/Redis/MinIO staging setup, backup/restore scripts, Paystack test-mode support, QR ticketing, organizer KYC/moderation, refunds, payouts, promo codes and admin settings.

## Important

- Do not commit real Paystack, SMTP, database, Redis or storage credentials.
- Use environment variables in Render.
- Keep `ALLOW_DEMO_PAYMENTS=true` only in staging.
- Before live payments, switch the repository to Private, complete QA, force HTTPS and disable demo payments.
