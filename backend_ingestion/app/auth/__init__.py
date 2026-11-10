"""Auth and RBAC (order 15).

Why a subpackage: `main.py` already carries WS/REST routing, lifespan, and
metrics; stuffing user models, JWT strategy, and role dependencies in would turn
it into a catch-all junk drawer. This module holds only auth-related things;
`main.py` plugs in via the two interfaces `auth.router` / `auth.deps`.
"""
