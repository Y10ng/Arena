export type AdminUserRole = 'user' | 'super_admin'

export interface AdminUser {
  id: string
  login: string
  role: AdminUserRole
  disabled: boolean
  must_change_password: boolean
  tenant_id: string | null
  active_session_count: number
  created_at: string
  updated_at: string
}

export interface CreateAdminUserRequest {
  login: string
}

export interface UpdateAdminUserStatusRequest {
  disabled: boolean
  expected_updated_at: string
}

export interface ResetAdminUserPasswordRequest {
  password: string
  expected_updated_at: string
}
