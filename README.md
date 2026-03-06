# Shapez - Visual Agent Factory System

A visual programming interface for Hermes Agent that displays tool usage,
message flows, and allows defining new sub-agents and prompts through a
factory/block-based UI.

## Concept

Shapez provides a two-way visual interface:

1. **Visualization Mode**: Watch Hermes Agent work in real-time
   - See tool calls as blocks flowing through a factory
   - Watch message exchanges between user and agent
   - Monitor background processes and their status
   - Track context compression events

2. **Factory Mode**: Define new agents and workflows
   - Create blocks that represent prompts or sub-agents
   - Connect blocks to define data/message flow
   - Save factories as reusable skills
   - Configure tool permissions per factory

## Architecture

```
shapez/
├── src/
│   ├── core/           # Core factory engine
│   │   ├── factory.py  # Factory definition and execution
│   │   ├── block.py    # Block base classes
│   │   └── connector.py # Block connection logic
│   ├── blocks/         # Built-in block types
│   │   ├── tool_block.py    # Tool invocation blocks
│   │   ├── agent_block.py   # Sub-agent blocks
│   │   ├── prompt_block.py  # Prompt template blocks
│   │   └── flow_block.py    # Control flow blocks
│   ├── ui/             # Terminal UI components
│   │   ├── canvas.py   # Main drawing canvas
│   │   ├── inspector.py # Block inspector panel
│   │   └── toolbar.py  # Tool palette
│   └── bridge/         # Hermes integration
│       ├── observer.py # Watch agent activity
│       └── executor.py # Execute factory definitions
├── blocks/             # User-defined block libraries
├── themes/             # UI color themes
└── factories/          # Saved factory definitions
```

## Integration with Hermes

Shapez integrates with Hermes Agent in two ways:

### Observer Mode
Attaches to a running Hermes Agent session and visualizes:
- Each tool call as a block activation
- Message flow between turns
- Sub-agent delegation as nested factories
- Background process status

### Factory Execution
Defined factories can be:
- Exported as Hermes skills (SKILL.md)
- Executed directly via the `execute_factory` tool
- Chained together for complex workflows

## Usage

```bash
# Start visualization mode
shapez observe --session <session_id>

# Open factory editor
shapez edit

# Run a factory
shapez run my_factory.json

# Export factory as skill
shapez export my_factory.json --output ~/.hermes/skills/
```

## Block Types

### Tool Block
Represents a single tool invocation with configurable parameters.

### Agent Block  
Spawns a sub-agent with its own system prompt and tool permissions.

### Prompt Block
A text template that can be parameterized and connected to other blocks.

### Router Block
Conditionally routes data based on content or metadata.

### Accumulator Block
Collects multiple inputs before passing to the next block.
