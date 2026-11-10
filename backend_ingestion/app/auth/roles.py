"""RBAC role constants (order 15 T3).

Why a separate module rather than writing it in config.py:
`role` must feed both the JWT signing side (written into claims at login) and the
verification side (read back for dependency injection); both must reference
**the same** source of truth. Scattering it as string literals would lead to
"signing writes admin, judging compares Admin", and that error would not surface
at startup -- it would silently turn admin into viewer.

Permission model (two levels, enough, no middle tier):
- VIEWER -- operator: read-only. Dashboard, 100-unit status, single-unit
  history, WS real-time stream.
- ADMIN  -- admin: all of VIEWER + write operations like order 17's alert
  acknowledge / suppression.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """The `str` mixin lets the enum value be serialized into claims by PyJWT
    directly, no manual str conversion."""

    VIEWER = "viewer"
    ADMIN = "admin"


#: Default role for newly registered users. Deliberately **not** admin --
#: self-service registration can only get read-only; privilege escalation must be
#: done by ops editing the DB or via the management interface, otherwise
#: `/auth/register` becomes a backdoor where anyone opens their own admin.
DEFAULT_ROLE = Role.VIEWER

#: The full set of roles allowed in the JWT. Used to validate before signing,
#: blocking hand-written claims carrying an out-of-range value.
ALL_ROLES: frozenset[Role] = frozenset(Role)
