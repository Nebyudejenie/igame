#!/usr/bin/env -S npx tsx
/**
 * Keno — standalone, offline round verifier.
 *
 * A 1:1 TypeScript port of packages/core/keno.py's commit-reveal draw
 * (_ByteStream, below(), derive_keno_draw, server_seed_hash) -- see
 * docs/keno/06-fairness-and-verification.md for the full explanation of
 * what this proves and why. Deliberately dependency-free (Node's
 * built-in `crypto` module only) so it can be run by anyone, on any
 * machine, with zero trust in this platform's own server: given only
 * the three published values below, it independently recomputes the
 * draw and tells you whether it matches.
 *
 * Usage — fully offline, no network call, no server trust at all:
 *
 *   npx tsx scripts/verify-round.ts \
 *     --server-seed <hex> \
 *     --public-seed <string> \
 *     --server-seed-hash <hex> \
 *     --drawn 3,12,17,29,...
 *
 * Usage — convenience mode, fetches a terminal round's own published
 * verification payload from the live API first (still re-derives and
 * checks it locally; the fetch only saves copy-pasting):
 *
 *   npx tsx scripts/verify-round.ts --round 1057 --api https://arada.click --auth "tma <initData>"
 *
 * Every gateway route requires a signed Telegram `Authorization: tma
 * <initData>` header, by platform-wide design (services/gateway/
 * app.py::_authenticated_user_id) -- not a Keno-specific restriction,
 * and this script cannot fabricate one on its own (nor should it be
 * able to: initData is signed by Telegram against the bot's own
 * token). --round mode is therefore only a convenience for someone who
 * already has a real Mini App session and wants to paste its initData
 * here instead of copying server_seed/public_seed/drawn_numbers by
 * hand. The offline mode above is the one that genuinely needs zero
 * trust in this platform at all -- given only the three published
 * values (which a Mini App session already displays once a round is
 * terminal), anyone can verify without ever talking to this server.
 *
 * Any TypeScript runner works identically (tsx, ts-node, bun, deno run
 * --allow-net --allow-read) -- there is nothing here beyond plain
 * Node-compatible TypeScript and the `crypto` builtin.
 *
 * Exit code 0 = verified (both the seed hash and the draw match);
 * non-zero = a mismatch or bad input. Prints a human-readable report
 * either way -- this is meant to be run by a person checking a specific
 * round, not silently piped.
 */

import { createHash, createHmac } from "node:crypto";

const NUMBER_POOL_SIZE = 80;
const DRAW_COUNT = 20;

// ---------------------------------------------------------------------------
// The draw itself -- byte-for-byte identical to packages/core/keno.py's
// _ByteStream/below()/derive_keno_draw. Do not "simplify" the rejection
// -sampling loop below into a modulo: modulo would silently bias low
// draw results whenever the pool size doesn't evenly divide the byte
// range, which is exactly the bias this algorithm exists to avoid.
// ---------------------------------------------------------------------------

class ByteStream {
  private counter = 0;
  private buffer = Buffer.alloc(0);

  constructor(private readonly serverSeed: Buffer, private readonly clientSeed: Buffer) {}

  take(n: number): Buffer {
    while (this.buffer.length < n) {
      const counterBytes = Buffer.alloc(4);
      counterBytes.writeUInt32BE(this.counter, 0);
      const block = createHmac("sha256", this.serverSeed)
        .update(Buffer.concat([this.clientSeed, counterBytes]))
        .digest();
      this.buffer = Buffer.concat([this.buffer, block]);
      this.counter += 1;
    }
    const chunk = this.buffer.subarray(0, n);
    this.buffer = this.buffer.subarray(n);
    return chunk;
  }

  /** Unbiased random int in [0, upperExclusive) via rejection sampling. */
  below(upperExclusive: number): number {
    if (upperExclusive <= 0) {
      throw new Error("upperExclusive must be positive");
    }
    const numBytes = Math.max(1, Math.floor(bitLength(upperExclusive - 1) / 8) + 1);
    const limit = 256 ** numBytes;
    const threshold = limit - (limit % upperExclusive);
    for (;;) {
      const value = this.take(numBytes).readUIntBE(0, numBytes);
      if (value < threshold) {
        return value % upperExclusive;
      }
    }
  }
}

/** Python int.bit_length() semantics: 0 -> 0, otherwise the number of
 * bits in the binary representation with no leading zeros. */
function bitLength(n: number): number {
  if (n === 0) return 0;
  return n.toString(2).length;
}

/** THE DRAW. Signature deliberately mirrors packages/core/keno.py's
 * derive_keno_draw exactly: (serverSeed, publicSeed, poolSize,
 * drawCount) -- no parameter through which ticket data could enter. */
function deriveKenoDraw(
  serverSeed: Buffer,
  publicSeed: string,
  poolSize: number = NUMBER_POOL_SIZE,
  drawCount: number = DRAW_COUNT,
): number[] {
  const stream = new ByteStream(serverSeed, Buffer.from(publicSeed, "utf-8"));
  const pool: number[] = [];
  for (let i = 1; i <= poolSize; i++) pool.push(i);
  const drawn: number[] = [];
  for (let i = 0; i < drawCount; i++) {
    const index = stream.below(pool.length);
    drawn.push(pool.splice(index, 1)[0]);
  }
  return drawn;
}

function serverSeedHash(serverSeed: Buffer): string {
  return createHash("sha256").update(serverSeed).digest("hex");
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function parseArgs(argv: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const value = argv[i + 1];
      if (value === undefined || value.startsWith("--")) {
        throw new Error(`--${key} needs a value`);
      }
      out[key] = value;
      i += 1;
    }
  }
  return out;
}

interface RoundPayload {
  id: number;
  status: string;
  server_seed_hash: string;
  server_seed: string | null;
  public_seed: string | null;
  drawn_numbers: number[] | null;
}

async function fetchRound(apiBase: string, roundId: string, auth: string | undefined): Promise<RoundPayload> {
  if (!auth) {
    throw new Error(
      "--round requires --auth 'tma <initData>' -- every gateway route needs a real, signed Telegram " +
        "session (see this file's own header comment for why). Use offline mode instead if you don't have one.",
    );
  }
  const url = `${apiBase.replace(/\/$/, "")}/api/keno/rounds/${roundId}`;
  const res = await fetch(url, { headers: { Authorization: auth } });
  if (!res.ok) {
    throw new Error(`GET ${url} -> HTTP ${res.status}`);
  }
  const body = (await res.json()) as RoundPayload;
  if (body.status !== "completed" && body.status !== "failed" && body.status !== "voided") {
    throw new Error(
      `round ${roundId} is not terminal yet (status=${body.status}) -- server_seed/drawn_numbers ` +
        `are withheld until settlement finishes; see docs/keno/06-fairness-and-verification.md`,
    );
  }
  if (body.server_seed === null || body.public_seed === null || body.drawn_numbers === null) {
    throw new Error(`round ${roundId} has no draw to verify (a voided/failed round before the draw ran)`);
  }
  return body;
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));

  let serverSeedHex: string;
  let publicSeed: string;
  let claimedHash: string | undefined;
  let claimedDrawn: number[] | undefined;
  let label: string;

  if (args.round) {
    const apiBase = args.api ?? "https://arada.click";
    const round = await fetchRound(apiBase, args.round, args.auth);
    serverSeedHex = round.server_seed as string;
    publicSeed = round.public_seed as string;
    claimedHash = round.server_seed_hash;
    claimedDrawn = round.drawn_numbers as number[];
    label = `round ${round.id} (fetched from ${apiBase})`;
  } else {
    if (!args["server-seed"] || !args["public-seed"]) {
      console.error(
        "Usage:\n" +
          "  verify-round.ts --server-seed <hex> --public-seed <string> [--server-seed-hash <hex>] [--drawn 1,2,3,...]\n" +
          "  verify-round.ts --round <id> --auth 'tma <initData>' [--api https://arada.click]\n",
      );
      process.exit(2);
    }
    serverSeedHex = args["server-seed"];
    publicSeed = args["public-seed"];
    claimedHash = args["server-seed-hash"];
    claimedDrawn = args.drawn ? args.drawn.split(",").map((s) => Number.parseInt(s.trim(), 10)) : undefined;
    label = "provided values";
  }

  const serverSeed = Buffer.from(serverSeedHex, "hex");
  const recomputedHash = serverSeedHash(serverSeed);
  const recomputedDraw = deriveKenoDraw(serverSeed, publicSeed, NUMBER_POOL_SIZE, DRAW_COUNT);

  console.log(`Verifying ${label}`);
  console.log(`  server_seed     : ${serverSeedHex}`);
  console.log(`  public_seed     : ${publicSeed}`);
  console.log(`  recomputed hash : ${recomputedHash}`);
  console.log(`  recomputed draw : [${recomputedDraw.join(", ")}]`);

  let ok = true;

  if (claimedHash !== undefined) {
    const hashMatches = recomputedHash.toLowerCase() === claimedHash.toLowerCase();
    console.log(`  claimed hash    : ${claimedHash}  ->  ${hashMatches ? "MATCH" : "MISMATCH"}`);
    ok = ok && hashMatches;
  } else {
    console.log("  (no --server-seed-hash given -- hash commitment not checked)");
  }

  if (claimedDrawn !== undefined) {
    const drawMatches =
      claimedDrawn.length === recomputedDraw.length && claimedDrawn.every((v, i) => v === recomputedDraw[i]);
    console.log(`  claimed draw    : [${claimedDrawn.join(", ")}]  ->  ${drawMatches ? "MATCH" : "MISMATCH"}`);
    ok = ok && drawMatches;
  } else {
    console.log("  (no --drawn given -- draw not checked against a claimed value)");
  }

  console.log(ok ? "\nVERIFIED" : "\nFAILED -- see mismatches above");
  process.exit(ok ? 0 : 1);
}

main().catch((err) => {
  console.error(`error: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
