# 2026-10-14 Xunjia Agent first remediation batch

Baseline: /home/anlin/work/xunjia-agent, local main @ 83c7e0f. Modifications are uncommitted/unpushed. No production service or actual account was started or altered.

## Implemented

- L2 human takeover lock: server/business.py _conversation now blocks ordinary confirmation regardless of HUMAN/STOPPED/STOPPING, until explicit resume.
- L3 late sent acknowledgment: receipt records real command/message evidence but leaves stopped/human session unchanged. Explicit resume obtains the latest trusted sent price for the same session to rehydrate the concession baseline.
- L5 stop wording: negative deal descriptions such as "还没成交" no longer blindly stop the bot; direct stop instructions still do.
- L4 missing counterparty volume: the chat gate refuses automatic price replies if no explicit counterparty volume. The separate once-only clarification action remains outstanding (safe handoff for now).
- L1 reference valuation: preview/confirm distinguish explicit absolute yield from BP-relative authorization. BP must use a trusted backend ReferenceSnapshot with verified issuer/source fields, maturity basis, fresh ChinaBond valuation date, saved BP floor and strategy. Confirm re-reads and compares snapshot/strategy. The Chat turn pins the original confirmed valuation, not a different refresh. Missing sources fail closed. Frontend shows the chosen mode and valuation date.
- STS production profile: server/transport_config.py requires expected_bucket for OSS; MQ plaintext HTTP is gated on explicit XUNJIA_ALLOW_INSECURE_MQ_HTTP=1 in development.
- scripts/sts_probe.py: local interactive, read-only STS test; secrets are prompted invisibly and never persisted; self-test uses synthetic values.

## Evidence

- New regression suite tests/acceptance/test_remediation_hardening_20261014.py verifies takeover/stop/reconfirm, late ACK and explicit resume, negated stop, quantity missing, verified BP preview/confirm, stale and altered source, strategy drift and signed BP. It also tests production STS profile config refusal and explicit dev-only opt-in.
- Full synthetic Python suite must be rerun after final test additions. Earlier run: 673 passed, 2 warnings, 0 failed. Frontend npm run build succeeded, npm test passed 8 standard + 44 developer tests.
- Isolated Python test dependencies are under /tmp/xunjia-remediation-testdeps-20261014; project .venv was left untouched.

## MQ STS network investigation

- Unauthenticated HTTP GET on the exact MQ endpoint returns HTTP 401, showing the route responds to auth attempts. This is NOT an authenticated result.
- HTTPS handshake succeeds at certificate-chain level, but hostname verification fails: CN *.yingmi-inc.com does not match nginx.mq.dq.yingmi-inc.com. Curl returns error 60. Disabling TLS verification is not an accepted fix.
- User-supplied STS secrets have not been stored in any script, repository file or log. Automated use of the secret values was blocked by the credential safety control; authenticated live STS success is therefore NOT claimed. No OSS request was made.
- To test interactively in the user's own trusted WSL shell:
  1) cd /home/anlin/work/xunjia-agent
  2) python3 scripts/sts_probe.py --self-test
  3) python3 scripts/sts_probe.py --url http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts --allow-insecure-dev-http
  The third command explicitly accepts development-only unencrypted HTTP; it prompts for the STS business key and original 16-byte SM4 key, reads response metadata without displaying temporary AK/SK/token, and never performs OSS GET/PUT. Prefer an IT-approved hostname-matched HTTPS endpoint without the insecure flag.

## Remaining gates before production

- Add once-only CLARIFY_VOLUME action, per-bond durable clarification budget, and Dify Chatflow contract support.
- Supply trusted read-only valuation provider and validate actual market data; no implicit BP authorization without it.
- Implement independent social consent and relation-memory controls, enrich the trader decision board.
- Integrate live v2 CollectorBridge/ReceiverLedger, real native execution evidence, dual transport stopping and out-of-order replay.
- Confirm production TLS STS endpoint, bucket, signing requirements and real authorized OSS/IMAP/SMTP interoperability.
- Decide legacy data import and S1/S2 deprecation; do not auto-migrate or restart old sessions.

Do not treat offline tests or isolated Dify simulation as production acceptance. No real IM messages were sent, no business data was changed, and no Git commit was created.
