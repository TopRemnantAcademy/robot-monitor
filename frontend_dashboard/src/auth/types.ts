// 认证相关类型（订单 15 T4）
// 字段名逐字对齐后端 app/auth/schemas.py:UserRead，禁止改名。

/** 两级 RBAC 角色。与后端 app/auth/roles.py:Role 一一对应。 */
export type Role = 'viewer' | 'admin';

/** `GET /users/me` 的响应。 */
export interface CurrentUser {
  id: number;
  email: string;
  is_active: boolean;
  is_verified: boolean;
  role: Role;
}

/** `POST /auth/jwt/login` 的响应。 */
export interface LoginResult {
  accessToken: string;
  tokenType: string;
}
