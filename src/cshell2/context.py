"""Context management — named collection with current pointer and push/pop stack."""

from dataclasses import dataclass, field


@dataclass
class Context:
    name: str
    variables: dict[str, str] = field(default_factory=dict)


class ContextManager:
    def __init__(self):
        self.contexts: dict[str, Context] = {}
        self.current_name: str | None = None
        self.stack: list[str] = []

    def create(self, name: str, **variables: str) -> Context:
        ctx = Context(name=name, variables=variables)
        self.contexts[name] = ctx
        if self.current_name is None:
            self.current_name = name
        return ctx

    def switch(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        self.current_name = name

    def push(self, name: str) -> None:
        if self.current_name is not None:
            self.stack.append(self.current_name)
        self.switch(name)

    def pop(self) -> Context | None:
        if not self.stack:
            self.current_name = None
            return None
        prev_name = self.stack.pop()
        self.current_name = prev_name
        return self.contexts.get(prev_name)

    def current(self) -> Context | None:
        if self.current_name is None:
            return None
        return self.contexts.get(self.current_name)

    def list_contexts(self) -> list[str]:
        return list(self.contexts.keys())

    def remove(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        del self.contexts[name]
        self.stack = [n for n in self.stack if n != name]
        if self.current_name == name:
            self.current_name = self.stack[-1] if self.stack else None

    def set_variable(self, key: str, value: str) -> None:
        ctx = self.current()
        if ctx is None:
            raise RuntimeError("No active context")
        ctx.variables[key] = value

    def get_variable(self, key: str) -> str | None:
        ctx = self.current()
        if ctx is None:
            return None
        return ctx.variables.get(key)
