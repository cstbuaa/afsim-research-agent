export interface StandaloneConfig {
  deploymentUrl: string;
  assistantId: string;
  langsmithApiKey?: string;
}

const CONFIG_KEY = "deep-agent-config";

// the local LangGraph server (`langgraph dev`, 127.0.0.1:2024) through this server's
// /langgraph proxy (same origin: no cross-origin request), and the graph in langgraph.json
const DIRECT = /^https?:\/\/(127\.0\.0\.1|localhost):2024\/?$/;

function proxied(): string {
  return typeof window === "undefined"
    ? "/langgraph"
    : `${window.location.origin}/langgraph`;
}

export const DEFAULT_CONFIG: StandaloneConfig = {
  deploymentUrl: "/langgraph",
  assistantId: "afsim_research",
};

function withDefaults(config: StandaloneConfig): StandaloneConfig {
  // a saved direct address of the local server goes through the proxy instead
  const url = config.deploymentUrl || "";
  return {
    ...config,
    deploymentUrl: !url || DIRECT.test(url) || url === "/langgraph" ? proxied() : url,
  };
}

export function getConfig(): StandaloneConfig | null {
  if (typeof window === "undefined") return null;

  let stored: string | null = null;
  try {
    stored = localStorage.getItem(CONFIG_KEY);
  } catch {
    return withDefaults(DEFAULT_CONFIG);
  }
  if (!stored) return withDefaults(DEFAULT_CONFIG);

  try {
    return withDefaults(JSON.parse(stored));
  } catch {
    return withDefaults(DEFAULT_CONFIG);
  }
}

export function saveConfig(config: StandaloneConfig): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
  } catch {
    // storage unavailable: the setting lasts for this page only
  }
}
