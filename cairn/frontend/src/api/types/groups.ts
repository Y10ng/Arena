export interface ProjectGroup {
  id: string;
  name: string;
  owner: string;
  created_at: string;
}

export interface CreateProjectGroupRequest {
  name: string;
}

export interface UpdateProjectGroupRequest {
  name: string;
}
