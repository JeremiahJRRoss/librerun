/**
 * Sealing a provider key to the gateway, in the browser (K7; L33, D15, D19,
 * D34).
 *
 * A provider key pasted in Application Settings never reaches the backend as
 * itself: it is encrypted here to the gateway's public key — RSA-OAEP with
 * SHA-256 for the digest and MGF1, a 3072-bit key, the label
 * `librerun-provider-key:v1:<name>` that binds the blob to its provider — and
 * only the gateway can open it (`services/gateway/gateway/sealing.py`, the
 * other end and the only RSA code the platform has). `openssl pkeyutl` with
 * the same parameters makes the same kind of blob, which is how the tests
 * and the secret sweep check both ends.
 *
 * WebCrypto's `subtle` exists only in a secure context — HTTPS, or
 * `localhost` — so on any other plain-HTTP origin the page explains, points
 * at gateway.env, and never sends anything (D19). One OAEP block carries at
 * most 318 bytes under this key, so a longer credential — a Vertex
 * service-account JSON is about 2.3 KB — stays in gateway.env (K7-16).
 *
 * The fingerprint an operator compares with the gateway's boot line (D34) is
 * `SHA256:` and the hex SHA-256 of the key's SubjectPublicKeyInfo DER.
 */

export const LABEL_PREFIX = "librerun-provider-key:v1:";
// One OAEP block under a 3072-bit key with SHA-256: 384 - 2·32 - 2 bytes.
export const MAX_KEY_BYTES = 318;

/** A key the page will not seal, said in words; never carries the key. */
export class SealError extends Error {}

/** Whether this page can seal at all: a secure context with WebCrypto. */
export function canSeal(): boolean {
  const scope = globalThis as { isSecureContext?: boolean; crypto?: Crypto };
  return scope.isSecureContext === true && typeof scope.crypto?.subtle?.encrypt === "function";
}

function derOf(pem: string): Uint8Array<ArrayBuffer> {
  const body = pem
    .split(/\r?\n/)
    .filter((line) => line.trim() && !line.startsWith("-----"))
    .join("")
    .trim();
  if (!body) throw new SealError("The gateway's public key is empty.");
  let binary: string;
  try {
    binary = atob(body);
  } catch {
    throw new SealError("The gateway's public key is not PEM.");
  }
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function hex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

/** `SHA256:<hex>` over the public key's SubjectPublicKeyInfo DER (D34). */
export async function sealingFingerprint(pem: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", derOf(pem));
  return `SHA256:${hex(digest)}`;
}

/**
 * The key as the gateway will take it: trimmed — a paste often carries a
 * newline — and 1 to 318 bytes of visible ASCII, which every provider's key
 * is. Anything else is refused here, in words, rather than sealed into a blob
 * the gateway would reject.
 */
export function keyBytes(key: string): Uint8Array<ArrayBuffer> {
  const value = key.trim();
  const encoded = new TextEncoder().encode(value);
  if (encoded.length === 0) throw new SealError("Paste a key first.");
  if (encoded.length > MAX_KEY_BYTES) {
    throw new SealError(
      `That is ${encoded.length} bytes; a key pasted here is at most ${MAX_KEY_BYTES}. ` +
        "A longer credential (a Vertex service-account JSON, say) goes in gateway.env as a file."
    );
  }
  for (const byte of encoded) {
    if (byte < 0x21 || byte > 0x7e) {
      throw new SealError("A provider key is one word of visible ASCII; this has a space or another character in it.");
    }
  }
  const bytes = new Uint8Array(new ArrayBuffer(encoded.length));
  bytes.set(encoded);
  return bytes;
}

/**
 * `key`, sealed for provider `name` to the public key `pem`, as base64 — the
 * body the backend stores and cannot open. Refuses (`SealError`) a key the
 * gateway would not take.
 */
export async function seal(pem: string, name: string, key: string): Promise<string> {
  const data = keyBytes(key);
  const publicKey = await crypto.subtle.importKey(
    "spki",
    derOf(pem),
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"]
  );
  const label = new TextEncoder().encode(LABEL_PREFIX + name);
  const sealed = await crypto.subtle.encrypt({ name: "RSA-OAEP", label }, publicKey, data);
  return base64(new Uint8Array(sealed));
}
