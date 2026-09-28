import { object, type JsonObject } from "./protocol.js";

interface SchemaConstraint {
  types?: string[];
  minLength?: number;
}
interface SchemaAlternative extends SchemaConstraint {
  required?: string[];
  properties?: Record<string, SchemaConstraint>;
}
interface SchemaRule {
  keyword: string;
  path: string;
  missing?: string[];
  allowed?: string[];
  required?: string[];
  not?: { required: string[] };
  types?: string[];
  alternatives?: SchemaAlternative[];
}
export interface SubmissionIssue { keyword: string; path: string; rules?: SchemaRule[] }
interface SchemaError { keyword: string; instancePath: string; schemaPath: string; params: Record<string, unknown> }
export interface SubmissionDiagnostics {
  issue?: SubmissionIssue;
  submission_attempts: number;
  repair_rounds: number;
  max_repair_rounds: number;
}
const KEYWORDS = new Set([
  "type", "required", "additionalProperties", "const", "enum", "anyOf", "oneOf", "allOf", "not", "if",
  "minLength", "maxLength", "pattern", "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
  "multipleOf", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
]);

function variants(schema: JsonObject, root: JsonObject, depth = 0): JsonObject[] {
  if (depth > 32) return [];
  const result = [schema];
  if (typeof schema.$ref === "string" && schema.$ref.startsWith("#/$defs/")) {
    const target = object(root.$defs) ? root.$defs[schema.$ref.slice(8)] : undefined;
    if (object(target)) result.push(...variants(target, root, depth + 1));
  }
  for (const key of ["anyOf", "oneOf", "allOf"]) {
    if (Array.isArray(schema[key])) for (const child of schema[key]) if (object(child)) result.push(...variants(child, root, depth + 1));
  }
  return result;
}

// required 只沿 $ref/allOf 的合取关系展开；oneOf/anyOf 的分支不能合并成共同要求。
function conjunctiveVariants(schema: JsonObject, root: JsonObject, depth = 0): JsonObject[] {
  if (depth > 32) return [];
  const result = [schema];
  if (typeof schema.$ref === "string" && schema.$ref.startsWith("#/$defs/")) {
    const target = object(root.$defs) ? root.$defs[schema.$ref.slice(8)] : undefined;
    if (object(target)) result.push(...conjunctiveVariants(target, root, depth + 1));
  }
  if (Array.isArray(schema.allOf)) for (const child of schema.allOf) if (object(child)) result.push(...conjunctiveVariants(child, root, depth + 1));
  return result;
}

const TYPES = new Set(["array", "boolean", "integer", "null", "number", "object", "string"]);
function schemaTypes(schema: JsonObject, root: JsonObject): string[] {
  return [...new Set(conjunctiveVariants(schema, root).flatMap((node) => typeof node.type === "string" && TYPES.has(node.type) ? [node.type] : []))].slice(0, 8);
}

function alternativeRule(schema: JsonObject, root: JsonObject, declared: string[]): SchemaAlternative | undefined {
  const nodes = conjunctiveVariants(schema, root);
  const alternative: SchemaAlternative = {};
  const types = schemaTypes(schema, root);
  if (types.length) alternative.types = types;
  const required = [...new Set(nodes.flatMap((node) => Array.isArray(node.required) ? node.required : []))]
    .filter((key): key is string => typeof key === "string" && declared.includes(key))
    .slice(0, 16);
  if (required.length) alternative.required = required;
  const properties: Record<string, SchemaConstraint> = {};
  for (const node of nodes) {
    if (!object(node.properties)) continue;
    for (const [key, value] of Object.entries(node.properties)) {
      if (!declared.includes(key) || !object(value) || Object.hasOwn(properties, key)) continue;
      const constraint: SchemaConstraint = {};
      const propertyTypes = schemaTypes(value, root);
      if (propertyTypes.length) constraint.types = propertyTypes;
      if (Number.isInteger(value.minLength) && (value.minLength as number) >= 0 && (value.minLength as number) <= 1_000_000) constraint.minLength = value.minLength as number;
      if (constraint.types || constraint.minLength !== undefined) properties[key] = constraint;
    }
  }
  if (Object.keys(properties).length) alternative.properties = properties;
  if (Number.isInteger(schema.minLength) && (schema.minLength as number) >= 0 && (schema.minLength as number) <= 1_000_000) alternative.minLength = schema.minLength as number;
  return Object.keys(alternative).length ? alternative : undefined;
}

// 只走 Schema 声明的属性；数组位置固定为 *，mapping key 与额外字段止于父路径。
function declaredLocation(schema: JsonObject, instancePath: string) {
  let nodes = [schema];
  const path: string[] = [];
  const segments = instancePath.split("/").slice(1);
  for (const encoded of segments.slice(0, 32)) {
    const segment = encoded.replace(/~1/g, "/").replace(/~0/g, "~");
    const expanded = nodes.flatMap((node) => variants(node, schema));
    const properties = expanded.flatMap((node) => object(node.properties) && Object.hasOwn(node.properties, segment) && object(node.properties[segment]) ? [node.properties[segment] as JsonObject] : []);
    if (properties.length) { path.push(encoded); nodes = properties; continue; }
    const items = expanded.flatMap((node) => object(node.items) ? [node.items] : []);
    if (/^(0|[1-9][0-9]*)$/.test(segment) && items.length) { path.push("*"); nodes = items; continue; }
    break;
  }
  return { path: path.length ? `/${path.join("/")}` : "$", nodes: nodes.flatMap((node) => variants(node, schema)), exact: path.length === segments.length };
}

function errorSchema(schema: JsonObject, schemaPath: string): JsonObject {
  let node: unknown = schema;
  // TypeBox 的错误路径展开了 $ref，沿冻结 Schema 解引用，不从候选补规则。
  for (const encoded of schemaPath.split("/").slice(1, 65)) {
    const key = encoded.replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(node)) node = node[Number(key)];
    else if (object(node)) node = variants(node, schema).find((variant) => Object.hasOwn(variant, key))?.[key];
    else return {};
  }
  return object(node) ? node : {};
}

export function safeSchemaIssue(schema: JsonObject, errors: SchemaError[]): SubmissionIssue {
  const error = errors.find((item) => item.keyword === "additionalProperties")
    ?? errors.find((item) => ["oneOf", "anyOf", "if"].includes(item.keyword))
    ?? errors[0];
  if (!error) return { keyword: "schema", path: "$" };
  const issue: SubmissionIssue = { keyword: KEYWORDS.has(error.keyword) ? error.keyword : "schema", path: declaredLocation(schema, error.instancePath).path };
  const rules: SchemaRule[] = [];
  const addRule = (rule: SchemaRule) => {
    if (!rules.some((prior) => JSON.stringify(prior) === JSON.stringify(rule))) rules.push(rule);
  };
  // 先保留组合约束，再补字段结构；同一数组位置统一为 * 后只输出一份规则。
  const ordered = [
    ...errors.filter((item) => item.keyword === "if"),
    ...errors.filter((item) => ["oneOf", "anyOf"].includes(item.keyword)),
    ...errors.filter((item) => ["required", "additionalProperties"].includes(item.keyword)),
    ...errors.filter((item) => item.keyword === "type"),
  ];
  for (const current of ordered) {
    if (rules.length >= 8) break;
    // 分支内部的 leaf error 只描述未选分支；父 oneOf/anyOf 才是完整合法选择。
    if (/\/(anyOf|oneOf)\/\d+(\/|$)/.test(current.schemaPath) && !["oneOf", "anyOf"].includes(current.keyword)) continue;
    const location = declaredLocation(schema, current.instancePath);
    if (!location.exact) continue;
    const declared = [...new Set(location.nodes.flatMap((node) => object(node.properties) ? Object.keys(node.properties) : []))].slice(0, 16);
    const fields = (value: unknown): string[] => Array.isArray(value) && value.length <= 16 && value.every((key) => typeof key === "string" && declared.includes(key)) ? value : [];
    const node = errorSchema(schema, current.schemaPath);
    const rule: SchemaRule = { keyword: current.keyword, path: location.path };
    if (current.keyword === "if") {
      const branch = current.params.failingKeyword === "then" ? node.then : current.params.failingKeyword === "else" ? node.else : undefined;
      if (!object(branch)) continue;
      const required = fields(branch.required);
      const forbidden = object(branch.not) ? fields(branch.not.required) : [];
      if (required.length) rule.required = required;
      // not.required 是组合约束，不能误说成其中每个字段都独立禁止。
      if (forbidden.length) rule.not = { required: forbidden };
      if (!rule.required && !rule.not) continue;
    } else if (["oneOf", "anyOf"].includes(current.keyword)) {
      const aggregate = conjunctiveVariants(node, schema).find((variant) => Array.isArray(variant[current.keyword]));
      const branchSchemas = aggregate?.[current.keyword];
      if (!Array.isArray(branchSchemas)) continue;
      const alternatives = branchSchemas
        .slice(0, 8)
        .flatMap((alternative: unknown) => object(alternative) ? [alternativeRule(alternative, schema, declared)] : [])
        .filter((alternative: SchemaAlternative | undefined): alternative is SchemaAlternative => alternative !== undefined);
      if (!alternatives.length) continue;
      rule.alternatives = alternatives;
    } else if (current.keyword === "type") {
      const types = schemaTypes(node, schema);
      if (!types.length) continue;
      rule.types = types;
    } else {
      const expanded = conjunctiveVariants(node, schema);
      const required = fields([...new Set(expanded.flatMap((variant) => Array.isArray(variant.required) ? variant.required : []))]);
      if (required.length) rule.required = required;
      if (declared.length <= 16 && expanded.some((variant) => variant.additionalProperties === false)) rule.allowed = declared;
      if (current.keyword === "required") rule.missing = fields(current.params.requiredProperties).filter((key) => required.includes(key));
      if (!rule.required && !rule.allowed && !rule.missing) continue;
    }
    addRule(rule);
  }
  return { ...issue, ...(rules.length ? { rules } : {}) };
}

export class SubmissionState {
  attempts = 0;
  repairs = 0;
  code = "submission_missing";
  issue: SubmissionIssue | undefined;
  constructor(readonly maxRepairs: number) {}
  reject(code: string, issue?: SubmissionIssue): void { this.code = code; this.issue = issue; }
  diagnostics(): SubmissionDiagnostics {
    return { ...(this.issue ? { issue: this.issue } : {}), submission_attempts: this.attempts, repair_rounds: this.repairs, max_repair_rounds: this.maxRepairs };
  }
  repairable(): boolean { return ["submission_missing", "submission_invalid", "artifact_unknown"].includes(this.code); }
}
