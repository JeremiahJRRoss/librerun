import type { AgentSettingSpec } from "../types";

/** One entry of the body `PUT /agents/{id}/config/settings` takes (K5a). */
export interface SettingUpdate {
  key: string;
  value: unknown;
}

/**
 * What the Settings tab PUTs when it saves (K5a, L32): `[{key, value}]`.
 *
 * The rules are stepPayload.ts's, for the same reasons.
 *
 * **Every key is sent.** The tab shows each setting's EFFECTIVE value —
 * the agent's default or this tenant's choice, in the same input — and
 * has no way to tell which is which, so it posts them all and the SERVER
 * decides what counts as a choice: a value equal to the manifest's
 * default is stored as nothing (D17). That is why the tab reads the
 * config back after a save rather than trusting what it sent.
 *
 * **A blank is a clear, and a clear has to be SENT.** An emptied input is
 * `null`, which returns the setting to its default; left out of the body
 * it would leave the stored value where it was.
 *
 * **`false` and `0` are values.** An unticked box and a number set to zero
 * are choices, not blanks: a truthiness test here would turn both into
 * clears, and a tenant could never set a flag off or a count to zero.
 */
export function settingUpdates(
  specs: AgentSettingSpec[],
  values: Record<string, unknown>,
): SettingUpdate[] {
  return specs.map((spec) => ({ key: spec.key, value: blankToNull(values[spec.key]) }));
}

/**
 * The body of one field's Reset: that setting alone, as `null` — "the
 * agent's default" — so the tenant's row is deleted and the next release's
 * default reaches this tenant again.
 */
export function settingReset(key: string): SettingUpdate[] {
  return [{ key, value: null }];
}

function blankToNull(value: unknown): unknown {
  return value === "" || value === undefined ? null : value;
}
