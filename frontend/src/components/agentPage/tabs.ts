/**
 * The agent page's tabs (K4b, D30): one registry, one line per tab.
 *
 * The page is a shell for every registered agent. A tab says what it
 * shows and when it is available, and the shell shows the available ones
 * — so the batches that add a tab (K8b "Secrets", K9 "Keys") each add a
 * line here, not a branch in the shell. No tab keys on an agent (L13):
 * availability reads only what the chassis serves for every agent alike.
 */
import type { ComponentType } from "react";
import type { AgentConfigResponse, UserProfile } from "../../types";
import { StepsTab } from "../AgentConfigEditor";
import { KeysTab } from "./KeysPanel";
import { SecretsTab } from "./SecretsPanel";
import { SettingsTab } from "./SettingsPanel";

export interface AgentPageData {
  agentId: string;
  user: UserProfile;
  /** The config GET's answer; null when it said 404 (nothing declared). */
  config: AgentConfigResponse | null;
}

export interface AgentPageTabProps {
  data: AgentPageData;
  /** Read the config again: after a save the server decides what counts
   * as an override, so the page shows its answer, not what was posted. */
  reload: () => Promise<void>;
}

export interface AgentPageTab {
  id: string;
  label: string;
  available: (data: AgentPageData) => boolean;
  component: ComponentType<AgentPageTabProps>;
}

export const AGENT_PAGE_TABS: AgentPageTab[] = [
  {
    id: "steps",
    label: "Steps",
    available: (data) => (data.config?.steps.length ?? 0) > 0,
    component: StepsTab,
  },
  {
    id: "settings",
    label: "Settings",
    available: (data) => (data.config?.meta.settings.length ?? 0) > 0,
    component: SettingsTab,
  },
  {
    id: "secrets",
    label: "Secrets",
    available: (data) => (data.config?.meta.secrets.length ?? 0) > 0,
    component: SecretsTab,
  },
  {
    id: "keys",
    label: "Keys",
    available: (data) => data.user.is_platform_admin,
    component: KeysTab,
  },
];
