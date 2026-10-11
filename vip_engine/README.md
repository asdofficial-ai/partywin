# Partywin VIP Engine — isolated V1 demo

**Status: QA prototype, NOT deployed and NOT integrated.** This folder is additive to the existing PARTYWIN Stage 8 ZIP on the staging branch. It does not modify the bundled server, routes, or production settings. Do not merge to production without integrating and reviewing the existing application's authentication, models, payment, notification and scanner systems.

## Run locally

- Python 3.11+.
- Install: pip install -r vip_engine/requirements.txt
- Set APP_ENV=development; VIP_DEMO_MODE=true; VIP_DEMO_ADMIN_KEY to a long random test secret; VIP_SCANNER_KEY to a separate long random test secret.
- Run: uvicorn vip_engine.app:app --host 127.0.0.1 --port 8000
- Open http://127.0.0.1:8000 (the server deliberately binds to localhost).

The sample maps two fictional venue tables (T12: 6 guests, ₦200,000 total; T13: 8 guests, ₦500,000 total). These are demonstration prices, not real inventory. The demo stores data in SQLite at VIP_DEMO_DB (default /tmp/partywin-vip-v1.sqlite3). Multiple instances or an ephemeral server filesystem are unsupported.

Use the frontend to reserve T12 for ten minutes, enter the operator-supplied demo admin secret, simulate a test confirmation, and view six independent QR passes. Door staff can enter the separately configured scanner key and scan or paste a token for one-time admission.

The demo uses an X-Guest-ID browser session identifier **only** for QA. It is not real identity verification and must be replaced with authenticated sessions / verified WhatsApp bindings for production.

## Implemented

- Interactive lightweight SVG seat picker with responsive layout.
- Server-authoritative pricing (kobo), 10-minute holds and atomic SQLite transaction conflict prevention.
- Retry-safe hold action keys (event, table, guest and unique action).
- Guest-scoped release and basic anti-hoarding limit.
- Local-only guarded simulated payment; no real Paystack charge.
- One random token per admitted seat, SHA-256 token digest stored at rest, QR SVG endpoint and atomic authenticated single-use check-in.
- Demonstration of late payment reconciliation and conflict exception generation.

## What is intentionally NOT implemented

- Production PostgreSQL migrations/row locks and webhook event deduplication.
- Paystack payment initialization, provider-authenticated webhooks, remote verification or refund processing.
- WhatsApp Cloud API, template approvals, opt-in storage, and notification delivery.
- Proper customer authentication, scanner staff roles, device revocation, and token delivery to individual guests.
- Audit trails, offline reconciliation, moderation, monitoring, PCI/security review and real venue data.

**Do not allow offline admission in V1.** Cache may explain a token's metadata, but an offline admission cannot guarantee single-use entry across multiple doors. Show "connection needed" and provide a supervised manual fallback process outside the automatic scanner.

When moving into PARTYWIN, use its actual PostgreSQL database and verified Paystack test mode. A provider-confirmed payment that happened before expiry may still conflict if inventory was resold while its webhook was delayed: record an exception and refund or resolve it without issuing two bookings. Repeated check-in must never admit twice.

## Test

pip install -r vip_engine/requirements.txt
pytest -q vip_engine/test_app.py

Keep the main Stage 8 app unchanged until review and integration are complete.
