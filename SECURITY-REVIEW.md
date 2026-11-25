# Security review: share links

Reviewed: the `share-links` branch (`d3171bc..0bd43a9`), before release. Reviewer: an independent
security review of the diff, 2026-11-25.

## Summary

| # | Severity | File | Lines | Finding | Confidence | Status |
|---|----------|------|-------|---------|------------|--------|
| 1 | Medium | `src/chiefofstaff/sharing.py` | 503–517, 589–600 | The private-data scan's *Remove* can leave a secret in the shared card, and common credential formats aren't flagged | 8/10 | Fixed in `ca1c426` |

Nothing else was found at this confidence. These held up:

- **Encryption:** AES-256-GCM with a fresh key and nonce for each link, so no nonce is reused. The version byte and the owner's GitHub id are bound into the payload.
- **The key:** it lives only in the link's fragment. It never reaches the Router, a worker, the logs or a server request.
- **Fetching a gist:** gist ids are validated, so a crafted link can't make Chief of Staff fetch another URL.
- **The viewer:** it renders through DOM APIs only, under a strict CSP with Trusted Types and no third-party code.
- **Imported cards:** they are quoted to the Router as someone else's words and never instruct workers.
- **New local endpoints:** every one requires the launch key and checks the request's Origin.
- **Revoke and expiry:** they delete only gists that Chief of Staff created for sharing.

## 1. The private-data scan can miss or only partly remove a secret

The scan is the only check before a card is published. Two weaknesses:

1. **Remove replaces only the matched text, not the whole secret.** `apply()` swaps each removed
   hit's matched value for `[removed]`, but several rules match only a marker next to the secret:
   - **A private key:** only the `-----BEGIN … PRIVATE KEY-----` line is matched. After Remove, the key's
     body and its END line stay in the card.
   - **AWS credentials:** only the access key id (`AKIA…`) is matched. The secret access key on the
     next line stays.
   - **A database URL** such as `postgres://user:password@db.internal/app`: only the internal host is
     matched, so the password stays. With a public host, nothing is flagged.
2. **Common credential lines aren't matched at all.** The rule starts with `\b`, and `_` is a word
   character, so `DB_PASSWORD=…`, `AWS_SECRET_ACCESS_KEY=…`, `SECRET_KEY=…` and `JWT_SECRET=…` never
   match. Quoted and JSON forms (`password = "…"`, `"password": "…"`) don't match either.

**Impact:** a worker's report that includes a generated deploy key or a `.env` excerpt is flagged in
part, or not at all. The owner clicks Remove, believes the secret is gone, and creates the link.
Anyone with the link can then read it, and a copy imported into another Chief of Staff keeps it even
after Revoke.

**Fix:**
- Match the whole private-key block (BEGIN through the matching END), AWS credential pairs, and
  `scheme://user:password@host` for any scheme, so that the removed value is the whole secret.
- Replace the leading `\b` with `(?<![A-Za-z0-9])`, and allow an optional quote around the key name
  and around the value.
- Add tests that check what remains after Remove, not only that something was detected.

**Fixed** in `ca1c426` ("Share links: Remove takes a secret out whole"), before share links ship:
- Each hit is now the whole secret. A private key runs from its `BEGIN` line to the matching
  `END`, including in a JSON string with escaped line breaks; when `END` is missing, it runs to
  the end of the body. An AWS key id is taken together with the secret within 200 characters of
  it, in either order. A link's `user:password@`, or just `:password@`, is found for any scheme.
- No rule starts with `\b`. Rules start with `(?<![A-Za-z0-9])`, and the credential rule allows a
  quote around the name and around the value. `DB_PASSWORD=`, `AWS_SECRET_ACCESS_KEY=`,
  `SECRET_KEY=`, `JWT_SECRET=`, `password = "…"`, `"password": "…"`, `client_secret:` and
  `x-api-key:` all match. Words about credentials don't: `secrets: inherit`, `max_tokens=…`,
  `tokenizer:`.
- `tests/test_share_remove.py` checks what is left after *Remove* for each case and for this
  review's own examples:
  - private keys: RSA, PKCS#8, EC, OpenSSH, encrypted, PGP, with headers, cut off, and in JSON;
  - AWS credentials in ini, env, prose and JSON;
  - nine links with a password, over eight schemes;
  - twelve credential-line forms.

  These tests failed before the fix: 35 of 45.
- `tests/test_share_routes.py` checks that the HTML file, the sealed card in the gist and a copy
  opened from the link all leave out the same secrets.
- Share…'s preview now draws the card as it will be shared, with each removed value replaced,
  and marks a secret that spans several lines on each of its lines. `scripts/ui_quality.py`
  checks this (`share/home-secrets-*`).
