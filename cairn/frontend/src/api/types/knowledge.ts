export interface KnowledgeSet {
  key: string;
  path: string;
  builtin: boolean;
  file_count: number;
  referenced_by: string[];
}

export interface KnowledgeFile {
  name: string;
  path: string;
  size: number;
  updated_at?: string | null;
  warnings: string[];
}

export interface KnowledgeFileContent extends KnowledgeFile {
  content: string;
}
