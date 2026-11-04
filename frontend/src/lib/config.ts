export interface StandaloneConfig {
  deploymentUrl: string;
  assistantId: string;
  langsmithApiKey?: string;
}

const CONFIG_KEY = "deep-agent-config";

// the local LangGraph server (`langgraph dev`) and the graph in langgraph.json
export const DEFAULT_CONFIG: StandaloneConfig = {
  deploymentUrl: "http://127.0.0.1:2024",
  assistantId: "afsim_research",
};

export function getConfig(): StandaloneConfig | null {
  if (typeof window === "undefined") return null;

  let stored: string | null = null;
  try {
    stored = localStorage.getItem(CONFIG_KEY);
  } catch {
    return DEFAULT_CONFIG;
  }
  if (!stored) return DEFAULT_CONFIG;

  try {
    return JSON.parse(stored);
  } catch {
    return DEFAULT_CONFIG;
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
