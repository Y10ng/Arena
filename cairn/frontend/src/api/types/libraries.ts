export interface AdvisoryLibrary {
  key: string;
  name: string;
  builtin: boolean;
  file_count: number;
  created_at: string;
  archived_at?: string | null;
}

export interface AdvisoryLibraryFile {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface LibraryManifest {
  library_key: string;
  library_name: string;
  file_count: number;
  files: AdvisoryLibraryFile[];
  safety_path: string | null;
}

export interface LibrarySelection {
  library_key: string;
  paths: string[] | null;
  source: 'current' | 'saved';
  expected_files?: Record<string, string> | null;
}

export interface CreateAdvisoryLibraryRequest {
  key: string;
  name?: string;
}
