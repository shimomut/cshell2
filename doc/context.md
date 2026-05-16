# Context System Design

## Overview

The context system provides named environments that bundle variables and a working directory. Switching contexts restores the associated state, making it easy to work across multiple environments (e.g., AWS accounts, projects, clusters) within a single shell session.

## Data Model

### Context

```python
@dataclass
class Context:
    name: str                           # unique identifier
    variables: dict[str, str]           # key-value pairs exported to os.environ
    cwd: str                            # saved working directory
```

A context captures:
- **Variables**: exported to `os.environ` on activation, restored on deactivation. Subprocesses inherit these automatically.
- **Working directory**: saved when leaving, restored when entering. Each context remembers where you were.

### ContextManager

```python
class ContextManager:
    contexts: dict[str, Context]     # all known contexts by name
    current_name: str | None         # which context is active
    stack: list[str]                 # name history for push/pop
```

The manager maintains:
- A **named collection** of all contexts (addressable by name)
- A **current pointer** indicating the active context
- A **stack** for push/pop navigation (stores names, not copies)

## Operations

### Create

```
context push prod --account 123456 --region us-east-1
```

Creates a context with the given name and variables. The `cwd` is captured at creation time. If this is the first context, it automatically becomes current.

### Switch

```
context switch staging
```

Directly sets the current pointer to any existing context. Does not modify the stack. The previous context remains available — nothing is lost.

### Push / Pop

```
context push staging    # saves current to stack, switches to staging
context pop             # returns to what was on top of stack
```

Push/pop provides stack-style navigation for temporary context switches. Push appends the current context name to the stack before switching. Pop removes the top of the stack and switches back.

When popping with an empty stack, the shell returns to a no-context state (no context variables, initial working directory).

### Remove

Deletes a context from the collection. If it was the current context, falls back to the top of the stack or no-context state.

## Environment Variable Management

### Activation

When a context becomes active:
1. Back up the current value of each variable key in `os.environ` (or `None` if unset)
2. Set each context variable in `os.environ`

### Deactivation

When a context is deactivated:
1. For each backed-up key, restore the original value (or remove if it was `None`)

This ensures:
- Context variables don't leak between contexts
- Pre-existing environment variables aren't permanently lost
- System commands spawned via subprocess see the correct variables

### Working Directory

On switch:
1. Save `os.getcwd()` into the departing context's `cwd`
2. `os.chdir()` to the arriving context's `cwd`

This means you can `cd` around within a context, switch away, and return to find yourself back where you left off.

## Integration with Completion

Completers receive the active context via `CompletionContext.shell_context`. This enables context-aware completions:

```python
class InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Use explicit arg if given, otherwise inherit from context
        account = ctx.shell_context.get_variable("account") if ctx.shell_context else None
        if ctx.args:
            account = ctx.args[0]
        # ... fetch completions for account
```

This pattern lets commands work both ways:
- Explicitly: `connect prod us-east-1` (args override)
- Implicitly: `connect` (inherits from active context)

## State Diagram

```
No Context ──create──→ Context A (current)
                           │
                       push B
                           │
                           ▼
              Context A ← stack ← Context B (current)
                           │
                         pop
                           │
                           ▼
              Context A (current), B still exists
                           │
                       switch B
                           │
                           ▼
              Context B (current), A still exists
```

## Prompt Integration

The shell prompt reflects the active context:

- No context: `dirname> `
- With context: `[contextname] dirname> `

This gives immediate visual feedback about which environment is active.

## Design Rationale

**Why named collection + stack instead of just a stack?**

A pure stack forces linear navigation — you must pop through intermediates to reach a distant context. Named contexts allow direct `switch` to any context at any time. The stack remains available as a convenience for the common "briefly enter context X, then return" pattern.

**Why export to os.environ?**

Subprocess commands (the system fallback) need to see context variables without cshell2-specific wiring. Exporting to `os.environ` means `aws`, `kubectl`, `ssh`, and other tools pick up the right account/region/cluster automatically.

**Why save cwd per context?**

Different environments often correspond to different directories (project roots, deployment repos). Saving cwd per context eliminates repetitive `cd` after every switch.
