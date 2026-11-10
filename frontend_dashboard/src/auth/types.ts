// Auth-related types (Order 15 T4)
// Field names are verbatim-aligned with the backend app/auth/schemas.py:UserRead; do not rename.

/** Two-level RBAC roles. Corresponds one-to-one with the backend app/auth/roles.py:Role. */
export type Role = 'viewer' | 'admin';

/** Response of `GET /users/me`. */
export interface CurrentUser {
  id: number;
  email: string;
  is_active: boolean;
  is_verified: boolean;
  role: Role;
}

/** Response of `POST /auth/jwt/login`. */
export interface LoginResult {
  accessToken: string;
  tokenType: string;
}
