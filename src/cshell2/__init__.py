"""cshell2 — a lightweight but powerful terminal shell environment."""

from .commands import arg, CmdParser, registry as command_registry
from .prompt import set_prompt
from .shell import passthrough_input, passthrough_run
from .variables import Var, EnvVar, registry as var_registry

__all__ = [
    "arg",
    "CmdParser",
    "set_prompt",
    "Var",
    "EnvVar",
    "var_registry",
    "command_registry",
    "passthrough_run",
    "passthrough_input",
]
