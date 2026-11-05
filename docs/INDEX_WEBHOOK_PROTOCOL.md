# Index webhook protocol (v1), as implemented

Source of truth: `coredevices/mobileapp`,
`experimental/src/commonMain/kotlin/coredevices/ring/external/indexwebhook/` (`INDEX_WEBHOOK_API.md`,
`IndexWebhookApi.kt`, `IndexWebhookSigning.kt`). Checked against the commit from 2026-11-04.

## Gestures → `X-Index-Trigger`

| Gesture | Trigger | Body |
|---|---|---|
| click | `click` | JSON button event |
| double click | `double-click` | JSON button event |
| triple click | `triple-click` | JSON button event |
| hold & talk | `single-click-hold` | multipart recording |
| double click & hold | `double-click-hold` | multipart recording |
| "Send test event" | `test-event` (+ `X-Index-Test: true`) | either, with `test` marker |

Each gesture has its own URL, headers, payload mode and signing secret.

## Button event (JSON)

```json
{"event":"button","gesture":"triple-click","clicks":3,"pressedAt":1700000000000,"client":"ring","payload":{"any":"json"}}
```

`payload` is the per-gesture text from the app settings. It is embedded as JSON when it parses,
as a string otherwise, and omitted when blank. Every press gets a fresh delivery UUID, so button
retries can't be deduplicated. That's harmless, because the app doesn't retry button events.

## Recording (multipart/form-data)

Fields: `audio` (AAC-LC M4A, mono 16 kHz, `audio/mp4`, filename `<recordingId>.m4a`),
`transcription` (text), `test` (only on test events), `recordedAt` (ms), `client`=`ring`.
`X-Audio-Size` is set when audio is present. `X-Index-Delivery` stays the same for every delivery
of one recording, and the app retries network errors, 408, 429 and 5xx every minute up to 10
times, including across app restarts. So **dedup by delivery id is required**, and ringhub keeps
ids for 7 days.

## Signature

```
X-Index-Signature = hex(HMAC_SHA256(utf8(secret),
    utf8("v1\n" + X-Index-Timestamp + "\n" + X-Index-Delivery + "\n" + X-Index-Trigger + "\n" + (test ? "1" : "0") + "\n")
    || raw_body))
```

`X-Index-Timestamp` is in Unix seconds. ringhub rejects requests more than 300 s away from its own
clock (configurable). Test events say `test-event` instead of the gesture, so ringhub checks them
against every configured secret.

## Status codes ringhub returns

`200` accepted, duplicate or test. `400`/`401`/`413`/`415` are permanent rejects, which the app
does not retry. ringhub never returns 5xx for bad input, so a malformed request can't trigger 10
retries.
