export interface ScenarioCatalogEntry {
  key: string;
  label: string;
  is_default: boolean;
  origin_template?: string | null;
  goal_template?: string | null;
}

export interface ScenarioFeatureSchemaItem {
  key: string;
  label: string;
  value_type: 'boolean' | 'enum' | 'string';
  default: boolean | string;
  options: string[];
  runtime_driver: boolean;
  description: string;
}

export interface Scenario {
  key: string;
  label: string;
  is_default: boolean;
  profile_id: string;
  // 兼容旧版已保存场景；新配置以 profile_id 为主。
  image: string;
  prompt_group: string;
  knowledge: string | null;
  origin_template?: string | null;
  goal_template?: string | null;
  cap_add: string[];
  features: Record<string, unknown>;
}

export interface ScenarioProfileOption {
  id: string;
  label: string;
  image_ref: string;
  image: string;
  network_policy: string;
  cap_add: string[];
  isolation: string;
  completed_action: string;
}

export interface ScenarioOptions {
  profiles: ScenarioProfileOption[];
  prompt_groups: string[];
  knowledge: string[];
  images: string[];
  cap_add: string[];
  cap_add_source: 'declared_inventory' | 'dispatcher_report' | 'unavailable';
  features: ScenarioFeatureSchemaItem[];
  project_counts: Record<string, number>;
}

export interface ScenarioValidationResult {
  valid: boolean;
  scenario_count: number;
  project_counts: Record<string, number>;
  warnings?: string[];
}

