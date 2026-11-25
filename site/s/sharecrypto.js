// Share links, format 1, in WebCrypto: the viewer's side. Chief's side is
// src/chiefofstaff/sharing.py (Python `cryptography`); both read and write the same bytes.
//
//   link     https://evanl666.github.io/chiefofstaff/s/#1.<gist id>.<key>.<expiry hint>
//   gist     one file, share.txt: base64url(0x01 · nonce[12] · AES-256-GCM ciphertext · tag[16])
//   AAD      "chiefofstaff-share:1:<GitHub user id of the gist's owner>"
//   payload  UTF-8 JSON, at most 64 KiB, with its own expires_at
//
// The key lives only in the fragment, which browsers never send. Every format a link made in the
// last 30 days could have is read here: a new one ships to this page 30 days before Chief writes it.

export const VERSION = 1;
export const MAX_PLAIN = 64 * 1024;
const MAX_BLOB = Math.ceil(((1 + 12 + MAX_PLAIN + 16) * 4) / 3);
const encoder = new TextEncoder();

export class DamagedError extends Error {}
export class NewerError extends Error {}

export function b64u(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export function unb64u(text) {
  if (typeof text !== "string" || !/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) throw new DamagedError("not base64url");
  const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}
export const aad = (ownerId) => encoder.encode(`chiefofstaff-share:${VERSION}:${ownerId}`);

async function aesKey(raw, use) {
  if (raw.length !== 32) throw new DamagedError("key length");
  return crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, [use]);
}

// Chief seals in Python; this is for the tests that hold both sides to the same bytes.
export async function sealWith(payload, ownerId, key, nonce) {
  const plain = encoder.encode(JSON.stringify(payload));
  if (plain.length > MAX_PLAIN) throw new RangeError(`payload is ${plain.length} bytes; at most ${MAX_PLAIN}`);
  const sealed = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce, additionalData: aad(ownerId), tagLength: 128 }, await aesKey(key, "encrypt"), plain));
  const blob = new Uint8Array(1 + 12 + sealed.length);
  blob.set([VERSION]);
  blob.set(nonce, 1);
  blob.set(sealed, 13);
  return { blob: b64u(blob), key: b64u(key) };
}
export const seal = (payload, ownerId) => sealWith(payload, ownerId, crypto.getRandomValues(new Uint8Array(32)), crypto.getRandomValues(new Uint8Array(12)));

// The payload, or DamagedError (anything changed, cut or not that owner's) / NewerError.
export async function open(blob, key, ownerId) {
  if (typeof blob !== "string" || blob.length > MAX_BLOB) throw new DamagedError("blob size");
  const bytes = unb64u(blob.trim());
  if (bytes.length < 1 + 12 + 16) throw new DamagedError("blob too short");
  if (bytes[0] !== VERSION) throw bytes[0] > VERSION ? new NewerError(`format ${bytes[0]}`) : new DamagedError("version");
  let plain;
  try {
    plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes.subarray(1, 13), additionalData: aad(ownerId), tagLength: 128 }, await aesKey(unb64u(key), "decrypt"), bytes.subarray(13));
  } catch (error) {
    if (error instanceof DamagedError) throw error;
    throw new DamagedError("authentication failed");
  }
  if (plain.byteLength > MAX_PLAIN) throw new DamagedError("payload size");
  let payload;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(plain));
  } catch {
    throw new DamagedError("not JSON");
  }
  if (!payload || payload.v !== VERSION || payload.kind !== "card") throw new DamagedError("not a card");
  return cleanPayload(payload);
}

// A payload's known fields, each checked for type and length before anything is drawn (as
// sharing.clean_payload): unknown ones are left out, a wrong one is damage.
const fail = (what) => {
  throw new DamagedError(what);
};
const text = (value, most, what) => (value == null ? "" : typeof value === "string" && value.length <= most ? value : fail(what));
const count = (value, what) => (value == null ? 0 : Number.isInteger(value) && value >= 0 && value < 1e9 ? value : fail(what));
const oneOf = (value, allowed, what) => (value == null || value === "" ? "" : allowed.includes(value) ? value : fail(what));
const list = (value, most, what) => (value == null ? [] : Array.isArray(value) && value.length <= most ? value : fail(what));
const object = (value, what) => (value && typeof value === "object" && !Array.isArray(value) ? value : fail(what));
const PR_URL = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+$/;
export function cleanCard(raw) {
  const card = object(raw, "card");
  const changes = object(card.changes || {}, "changes");
  const at = text(card.at, 40, "at");
  if (at && !/^[0-9a-f]{4,40}$/.test(at)) fail("at");
  return {
    title: text(card.title, 300, "title") || "A shared card",
    status: oneOf(card.status, ["review", "done"], "status"),
    status_words: text(card.status_words, 60, "status words"),
    finished_at: text(card.finished_at, 40, "finished_at"),
    result: text(card.result, 40100, "result"),
    verdict: oneOf(card.verdict, ["verified", "partial", "unverified", "failed"], "verdict"),
    criteria: list(card.criteria, 50, "criteria").map((item) => ({
      text: text(object(item, "criterion").text, 2000, "criterion"),
      state: oneOf(item.state, ["verified", "claimed", "contradicted", "not_met", "not_checked"], "criterion state"),
      check: text(item.check, 2000, "criterion check"),
    })),
    checks: list(card.checks, 3, "checks").map((item) => ({
      label: oneOf(object(item, "check").label, ["tests", "checks", "build"], "check"),
      command: text(item.command, 2000, "check command"),
      outcome: oneOf(item.outcome, ["passed", "failed", "running", "none"], "check outcome"),
      failed_before: Boolean(item.failed_before),
      runs: count(item.runs, "runs"),
    })),
    changes: Object.fromEntries(["files", "insertions", "deletions", "commits"].map((key) => [key, count(changes[key], key)])),
    at,
    prs: list(card.prs, 10, "prs").map((item) => {
      const url = text(object(item, "pr").url, 300, "pr url");
      if (!PR_URL.test(url)) fail("pr url");
      return {
        label: text(item.label, 300, "pr label"),
        url,
        title: text(item.title, 300, "pr title"),
        state: oneOf(item.state, ["OPEN", "CLOSED", "MERGED"], "pr state"),
        checks: oneOf(item.checks, ["passing", "failing", "pending", "none"], "ci"),
        review: oneOf(item.review, ["APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"], "review"),
      };
    }),
    flags: list(card.flags, 30, "flags").map((flag) => text(flag, 2000, "flag")),
  };
}
export function cleanPayload(payload) {
  const by = object(payload.by || {}, "by");
  for (const key of ["created_at", "expires_at"]) if (!Number.isFinite(zoned(payload[key]))) fail(key);
  return {
    v: VERSION,
    kind: "card",
    created_at: payload.created_at,
    expires_at: payload.expires_at,
    by: { login: text(by.login, 100, "login"), id: count(by.id, "id") },
    app: text(payload.app, 40, "app"),
    card: cleanCard(payload.card),
  };
}

// A link as pasted (the whole address, or its fragment), or null; `{ newer }` for a later format.
const FRAGMENT = /^(\d+)\.([0-9a-f]{20,40})\.([A-Za-z0-9_-]{43})(?:\.([0-9a-z]{1,9}))?$/;
export function parseLink(value) {
  const whole = String(value || "").trim();
  const hash = whole.includes("#") ? whole.slice(whole.indexOf("#") + 1) : whole;
  const found = FRAGMENT.exec(hash);
  if (!found) return null;
  const version = Number(found[1]);
  if (version !== VERSION) return version > VERSION ? { newer: true } : null;
  const hint = found[4] ? new Date(parseInt(found[4], 36) * 1000).toISOString() : "";
  return { gistId: found[2], key: found[3], expiresHint: hint };
}
// An ISO time with its zone, in ms (NaN otherwise: a time without one is no time).
const zoned = (value) => (typeof value === "string" && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? Date.parse(value) : NaN);
// Past its own expiry (or with none that parses): never shown, whatever GitHub still holds.
export const expired = (payload, now = Date.now()) => !(zoned(payload?.expires_at) > now);
