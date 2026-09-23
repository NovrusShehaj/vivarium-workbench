// AUTO-GENERATED from vivarium_workbench_assistant/api_models.py — do not edit by hand.
// Regenerate: python -m vivarium_workbench_assistant.generate_ts

export interface ContextSpec {
  kind: 'page_summary' | 'study' | 'investigation' | 'composite' | 'run_log' | 'git_diff' | 'manifest' | 'file' | 'search' | 'paste';
  page: string | null;
  investigation: string | null;
  study: string | null;
  composite: string | null;
  slug: string | null;
  id: string | null;
  run_id: string | null;
  tail_lines: number | null;
  paths: string[] | null;
  path: string | null;
  include_ignored: boolean;
  query: string | null;
  glob: string | null;
  max_results: number | null;
  label: string | null;
  text: string | null;
}

export interface RunRequest {
  action: 'send' | 'retry' | 'regenerate';
  message: string | null;
  parent_id: string | null;
  context: ContextSpec[] | null;
  provider_instance: string;
  model: string;
  agent: boolean;
}

export interface ConversationCreate {
  title: string | null;
}

export interface ConversationPatch {
  title: string;
}

export interface DeleteAllRequest {
  confirm: string;
}

export interface CredentialRefBody {
  source: 'keyring' | 'env' | 'session' | 'adc' | 'key_file' | 'none';
  env_var: string | null;
  key_file_path: string | null;
}

export interface InstanceCreate {
  id: string | null;
  type: 'anthropic' | 'openai' | 'vertex' | 'ai_studio' | 'openrouter' | 'openai_compatible';
  display_name: string | null;
  credential: CredentialRefBody | null;
  base_url: string | null;
  preset: 'ollama' | 'lm_studio' | 'custom' | null;
  project: string | null;
  location: string | null;
  default_model: string | null;
  manual_models: string[];
  first_token_timeout_s: number | null;
  ca_bundle_path: string | null;
  send_attribution_headers: boolean;
  context_window_override: number | null;
}

export interface InstancePatch {
  display_name: string | null;
  enabled: boolean | null;
  credential: CredentialRefBody | null;
  base_url: string | null;
  preset: 'ollama' | 'lm_studio' | 'custom' | null;
  project: string | null;
  location: string | null;
  default_model: string | null;
  manual_models: string[] | null;
  first_token_timeout_s: number | null;
  ca_bundle_path: string | null;
  send_attribution_headers: boolean | null;
  context_window_override: number | null;
  make_default: boolean | null;
}

export interface CredentialSet {
  api_key: string | null;
  access_token: string | null;
  session_only: boolean;
}

export interface CredentialStatusView {
  configured: boolean;
  source: string;
  hint: string | null;
  notice: string | null;
}

export interface ApprovalDecision {
  decision: 'approve' | 'deny';
  scope: 'once' | 'conversation';
  args_hash: string;
}

export interface ProposalAction {
  paths: string[] | null;
  commit: boolean;
}

export interface PreferencesPatch {
  default_instance: string | null;
  auto_context: 'off' | 'page_summary' | 'page_and_selection' | null;
  confirm_first_cloud_send: boolean | null;
  persist_conversations: boolean | null;
  retention_days: number | null;
  system_prompt_extra: string | null;
  tools: Record<string, 'auto' | 'ask' | 'disabled'> | null;
  panel_shortcut: string | null;
}

export interface ContextPreviewRequest {
  context: ContextSpec[];
  provider_instance: string | null;
  model: string | null;
  message: string | null;
}

export interface SuggestBody {
  kind: 'repo-name' | 'pr-title' | 'pr-body';
  provider_instance: string;
  model: string;
}

export interface ModelCapabilitiesBody {
  model: string;
  tools: boolean | null;
  context_window: number | null;
  max_output_tokens: number | null;
}

export interface AssistantStatus {
  enabled: boolean;
  available: boolean;
  reason: string;
  mode: string;
  capabilities: Record<string, any>;
  providers_configured: number;
  default_instance: string | null;
  persist_conversations: boolean;
  keyring_available: boolean;
  google_auth_installed: boolean;
  config_read_only: string | null;
  storage: Record<string, string>;
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: number | null;
  updated_at: number | null;
  message_count: number;
}

export interface ContextManifestEntry {
  id: string;
  kind: string;
  label: string;
  tokens: number;
  truncated: boolean;
  path: string | null;
  sha256: string | null;
  flags: string[];
  dropped: boolean;
  error: string | null;
  needs_confirmation: string | null;
}

export interface ContextPreview {
  items: ContextManifestEntry[];
  total_tokens: number;
  budget_tokens: number;
  warnings: string[];
  locality: string | null;
  provider: string | null;
  model: string | null;
}

export interface StreamEventRunStart {
  v: number;
  run_id: string;
  conversation_id: string;
  message_id: string;
  user_message_id: string;
  provider_instance: string;
  model: string;
  locality: string;
  context_manifest: ContextManifestEntry[];
  warnings: string[];
  agent: boolean;
}
