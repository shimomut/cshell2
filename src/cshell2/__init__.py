"""cshell2 — a lightweight but powerful terminal shell environment."""

from .prompt import set_prompt
from .variables import Var, EnvVar, var_registry

__all__ = ["set_prompt", "Var", "EnvVar", "var_registry"]
