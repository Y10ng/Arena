export interface AdvisoryLibrary {
  key: string;
  name: string;
  latest_version?: number | null;
  file_count: number;
  created_at: string;
  archived_at?: string | null;
}

export interface AdvisoryLibraryRevision {
  library_key: string;
  version: number;
  digest_sha256: string;
  source: 'api' | 'legacy_filesystem';
  message?: string | null;
  created_by?: string | null;
  created_at: string;
  file_count: number;
}

export interface AdvisoryLibraryFile {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface AdvisoryLibraryFileContent extends AdvisoryLibraryFile {
  content: string;
}

export interface CreateAdvisoryLibraryRequest {
  key: string;
  name?: string;
}

export interface CreateAdvisoryLibraryRevisionRequest {
  files: Array<{ path: string; content: string }>;
  message?: string;
}
