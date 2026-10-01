// @vitest-environment node
/**
 * Sealing a provider key in the browser (K7; L33, D15, D34), held to
 * `openssl`, which the gateway's own suite holds to `cryptography` — so the
 * browser, the sweep's canary and the gateway agree on one blob.
 *
 * Node, not jsdom: jsdom has no SubtleCrypto, and Node's WebCrypto is the
 * same API the page calls. Two vectors:
 *
 * - the fingerprint of a public key `openssl` made, against the digest
 *   `openssl pkey -pubin -outform DER | sha256sum` printed for it (D34);
 * - a key sealed by `seal()` to a keypair made here, opened by `openssl
 *   pkeyutl -decrypt` with the gateway's parameters and the label — and not
 *   opened under another provider's label (K7-04). The private half lives in
 *   a temporary file for the one command, and nowhere in the tree.
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { LABEL_PREFIX, MAX_KEY_BYTES, SealError, canSeal, keyBytes, seal, sealingFingerprint } from "../sealing";

// Made with `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072`
// then `openssl pkey -pubout`; its private half was deleted at once.
const OPENSSL_PUBLIC = `-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAqwNRUrkatvOAbn3udXau
PcRr8dfvHooIgJFMx8KJ2quUD+Zygl+a78RZ7Po6U9wo1++Wx+FJqXmSb94NRa8U
c/CxiXwaHQGVbHKDWTSa8PF6zub+BaFEM0TKHrjfxabY2IW6kS0wiKh1SFCS3NZe
qW+3Uy90g/56pAeY3rtIFI3vvEVQrouX62ZqFcmkSn7IyyyM296biQ3iUJ6dF/Vu
oVKdxqIOgRLmv6V8cr/7WpKqXEu9QHXOIBT2PeC4sMPaj7i5IKII/51XMV28YT9j
cQ8kJQV35jqre4PsyO0tfxAmOcQVUy/X+qPMxh41NdKcYONx4B7t0VzEAG+lTpKI
aZXiL2ZmGDhHmwJnQCoDPNN5jw2XF0LJ0zuc8511sPEEJ443oER8Khc4/2oQTbK/
jiITDb9tn5O9U5ex2phTqTXYtj2rv4C7fOJgCfbPWvvY8ShS8pPRJSo0EYE8HuXn
8EKPlBNQARg9zv4f2S3gxVE2UzzF3OxX11NYpgTQVGZ/AgMBAAE=
-----END PUBLIC KEY-----
`;
// `openssl pkey -pubin -in public.pem -outform DER | sha256sum`
const OPENSSL_FINGERPRINT = "SHA256:6b0a1d84762e366d4140059d52833cace6ee4641c1402dac632a7312967ced60";

function pem(kind: "PUBLIC" | "PRIVATE", der: ArrayBuffer): string {
  const body = Buffer.from(der).toString("base64").replace(/(.{64})/g, "$1\n");
  return `-----BEGIN ${kind} KEY-----\n${body.trim()}\n-----END ${kind} KEY-----\n`;
}

async function keypair(): Promise<{ publicPem: string; privatePem: string }> {
  const pair = await crypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"]
  );
  return {
    publicPem: pem("PUBLIC", await crypto.subtle.exportKey("spki", pair.publicKey)),
    privatePem: pem("PRIVATE", await crypto.subtle.exportKey("pkcs8", pair.privateKey)),
  };
}

function opensslOpen(privatePem: string, name: string, sealedB64: string): string {
  const dir = mkdtempSync(join(tmpdir(), "k7-sealing-"));
  try {
    writeFileSync(join(dir, "private.pem"), privatePem, { mode: 0o600 });
    writeFileSync(join(dir, "blob.bin"), Buffer.from(sealedB64, "base64"));
    return execFileSync(
      "openssl",
      [
        "pkeyutl", "-decrypt", "-inkey", join(dir, "private.pem"),
        "-pkeyopt", "rsa_padding_mode:oaep",
        "-pkeyopt", "rsa_oaep_md:sha256",
        "-pkeyopt", "rsa_mgf1_md:sha256",
        "-pkeyopt", `rsa_oaep_label:${Buffer.from(LABEL_PREFIX + name).toString("hex")}`,
        "-in", join(dir, "blob.bin"),
      ],
      { stdio: ["ignore", "pipe", "pipe"] }
    ).toString();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

describe("sealingFingerprint", () => {
  it("is openssl's digest of the SubjectPublicKeyInfo DER", async () => {
    expect(await sealingFingerprint(OPENSSL_PUBLIC)).toBe(OPENSSL_FINGERPRINT);
  });

  it("refuses what is not PEM", async () => {
    await expect(sealingFingerprint("-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n")).rejects.toThrow(
      SealError
    );
  });
});

describe("seal", () => {
  it("makes a 384-byte blob openssl opens with the gateway's parameters and this provider's label", async () => {
    const { publicPem, privatePem } = await keypair();
    const key = "sk-ant-api03-the-browser-sealed-this";
    const sealed = await seal(publicPem, "anthropic", `  ${key}\n`);

    expect(Buffer.from(sealed, "base64")).toHaveLength(384);
    expect(sealed).not.toContain(key);
    expect(opensslOpen(privatePem, "anthropic", sealed)).toBe(key);
    // Bound to its provider: another label does not open it.
    expect(() => opensslOpen(privatePem, "openai", sealed)).toThrow();
  });

  it("seals the longest key one block carries, and no longer", async () => {
    const { publicPem, privatePem } = await keypair();
    const longest = "k".repeat(MAX_KEY_BYTES);
    expect(opensslOpen(privatePem, "google", await seal(publicPem, "google", longest))).toBe(longest);
    await expect(seal(publicPem, "google", `${longest}k`)).rejects.toThrow(/at most 318/);
  });

  it("refuses an empty key or one that is not visible ASCII, and never quotes it", () => {
    expect(() => keyBytes("   ")).toThrow(SealError);
    for (const bad of ["two words", "sk-ü-nicode", "tab\there"]) {
      let message = "";
      try {
        keyBytes(bad);
      } catch (error) {
        message = (error as Error).message;
      }
      expect(message).toMatch(/visible ASCII/);
      expect(message).not.toContain(bad);
    }
  });
});

describe("canSeal", () => {
  it("is false outside a secure context, whatever WebCrypto there is", () => {
    // Node has crypto.subtle and no isSecureContext: the page's D19 branch.
    expect(typeof globalThis.crypto?.subtle?.encrypt).toBe("function");
    expect(canSeal()).toBe(false);
    const scope = globalThis as { isSecureContext?: boolean };
    scope.isSecureContext = true;
    try {
      expect(canSeal()).toBe(true);
    } finally {
      delete scope.isSecureContext;
    }
  });
});
