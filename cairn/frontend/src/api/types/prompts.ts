export type PromptGroupCapability = 'exploration' | 'durable-report';
export type PromptGroupVisibility = 'visible' | 'compatibility-only' | 'test-only';

export interface PromptGroup {
  key: string;
  path: string;
  builtin: boolean;
  managed: boolean;
  file_count: number;
  referenced_by: string[];
  overrides_builtin: boolean;
  capabilities: PromptGroupCapability[];
  visibility: PromptGroupVisibility;
  valid: boolean;
  warnings: string[];
}

export interface PromptFile {
  name: string;
  path: string;
  size: number;
  updated_at?: string | null;
  required_tokens: string[];
  missing_tokens: string[];
}

export interface PromptFileContent extends PromptFile {
  content: string;
}

export interface PromptGroupValidationResult {
  valid: boolean;
  missing_files: string[];
  missing_tokens: Record<string, string[]>;
  warnings: string[];
}
