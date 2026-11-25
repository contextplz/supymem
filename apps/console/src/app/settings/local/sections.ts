export type SettingsField = {
  name: string;
  label: string;
  secret?: boolean;
  type?: "number" | "url" | "textarea";
  purpose: string;
  capabilities: string[];
  url: string;
  data_sent: string;
  required: boolean;
  state: "set" | "not set" | "from .env" | "unreadable";
  env: string[];
  options?: { value: string; label: string }[];
  providers?: string[];
  help_text?: string;
  min?: number;
  max?: number;
  step?: string;
};

export type LocalSection = {
  id: string;
  title: string;
  description: string;
  path: string;
  fields: SettingsField[];
  state: "ready" | "needs_setup" | "saved_not_tested";
  next_step: string;
  missing?: string[];
  url: string;
  help_text: string;
  unlocks: string[];
  data_sent: string;
  provider_urls?: Record<string, string>;
};

export type SettingsCatalog = { env_path: string; sections: LocalSection[]; import_error?: string | null; storage_notice?: string };
