"""
Factory - The main container for block-based workflows.

A factory defines a complete agent workflow consisting of:
- Blocks (operations)
- Connections (data flow)
- Configuration (execution settings)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .block import Block, BlockType, create_tool_block, create_agent_block
from .connector import Connector, Connection

logger = logging.getLogger(__name__)


class FactoryState(Enum):
    """Execution state of a factory."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class FactoryConfig:
    """Configuration for factory execution."""
    # Execution settings
    max_iterations: int = 100
    timeout_seconds: float = 300.0
    parallel_execution: bool = True
    
    # Error handling
    stop_on_error: bool = False
    retry_count: int = 0
    
    # Visualization
    animate: bool = True
    step_delay: float = 0.1


@dataclass
class ExecutionContext:
    """Runtime context for factory execution."""
    factory_id: str
    start_time: float = 0.0
    iteration: int = 0
    
    # Block execution order
    execution_order: List[str] = field(default_factory=list)
    
    # Results
    outputs: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    
    # Callbacks
    on_block_start: Optional[Callable[[Block], None]] = None
    on_block_complete: Optional[Callable[[Block, Any], None]] = None
    on_block_error: Optional[Callable[[Block, Exception], None]] = None


class Factory:
    """
    A factory defining a block-based workflow.
    
    Factories are the top-level container that holds blocks and their
    connections. They can be:
    - Created visually in the UI
    - Loaded from JSON files
    - Executed to run the workflow
    - Exported as Hermes skills
    """
    
    def __init__(
        self,
        name: str = "Untitled Factory",
        description: str = "",
    ):
        self.id = str(time.time_ns())
        self.name = name
        self.description = description
        
        # Blocks indexed by ID
        self.blocks: Dict[str, Block] = {}
        
        # Connection manager
        self.connector = Connector()
        
        # Execution configuration
        self.config = FactoryConfig()
        
        # Current state
        self.state = FactoryState.IDLE
        self._context: Optional[ExecutionContext] = None
        
        # Hermes integration
        self._agent = None  # AIAgent instance when executing
        self._tool_handler = None  # Custom tool handler
    
    def add_block(self, block: Block) -> Block:
        """Add a block to the factory."""
        self.blocks[block.id] = block
        return block
    
    def remove_block(self, block_id: str) -> bool:
        """Remove a block and its connections."""
        if block_id not in self.blocks:
            return False
        
        block = self.blocks[block_id]
        
        # Remove connections to/from this block
        to_remove = []
        for conn_id, conn in self.connector.connections.items():
            if conn.source_block_id == block_id or conn.target_block_id == block_id:
                to_remove.append(conn_id)
        
        for conn_id in to_remove:
            self.connector.disconnect(conn_id)
        
        del self.blocks[block_id]
        return True
    
    def connect(
        self,
        source_block_id: str,
        source_port: str,
        target_block_id: str,
        target_port: str,
    ) -> Optional[Connection]:
        """Create a connection between two blocks."""
        source = self.blocks.get(source_block_id)
        target = self.blocks.get(target_block_id)
        
        if not source or not target:
            return None
        
        return self.connector.connect(source, source_port, target, target_port)
    
    def get_input_blocks(self) -> List[Block]:
        """Get all blocks with no required unsatisfied inputs (entry points)."""
        result = []
        for block in self.blocks.values():
            if block.block_type == BlockType.INPUT:
                result.append(block)
            elif not any(
                p.required and not p.connected 
                for p in block.inputs
            ):
                # Has no required unconnected inputs
                has_connected_inputs = any(p.connected for p in block.inputs)
                if not has_connected_inputs and block.inputs:
                    result.append(block)
        return result
    
    def get_output_blocks(self) -> List[Block]:
        """Get all blocks that output results (exit points)."""
        result = []
        for block in self.blocks.values():
            if block.block_type == BlockType.OUTPUT:
                result.append(block)
            elif not any(p.connected for p in block.outputs):
                # Has unconnected outputs
                result.append(block)
        return result
    
    def topological_sort(self) -> List[str]:
        """
        Sort blocks in execution order (topological sort).
        
        Returns block IDs in order they should be executed.
        """
        # Build adjacency list
        in_degree = {bid: 0 for bid in self.blocks}
        adj = {bid: [] for bid in self.blocks}
        
        for conn in self.connector.connections.values():
            if conn.target_block_id in in_degree:
                in_degree[conn.target_block_id] += 1
            if conn.source_block_id in adj:
                adj[conn.source_block_id].append(conn.target_block_id)
        
        # Kahn's algorithm
        queue = [bid for bid, deg in in_degree.items() if deg == 0]
        result = []
        
        while queue:
            # Sort queue for deterministic order
            queue.sort()
            block_id = queue.pop(0)
            result.append(block_id)
            
            for neighbor in adj[block_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        return result
    
    async def execute(
        self,
        inputs: Dict[str, Any] = None,
        agent = None,
        on_block_start: Callable[[Block], None] = None,
        on_block_complete: Callable[[Block, Any], None] = None,
    ) -> Dict[str, Any]:
        """
        Execute the factory workflow.
        
        Args:
            inputs: Initial values to inject into input blocks
            agent: AIAgent instance for tool/agent blocks
            on_block_start: Callback when a block starts executing
            on_block_complete: Callback when a block completes
        
        Returns:
            Dict of output block results
        """
        self.state = FactoryState.RUNNING
        self._agent = agent
        
        # Initialize context
        self._context = ExecutionContext(
            factory_id=self.id,
            start_time=time.time(),
            on_block_start=on_block_start,
            on_block_complete=on_block_complete,
        )
        
        # Reset all blocks
        for block in self.blocks.values():
            block.reset()
        
        # Inject inputs
        if inputs:
            for block_name, value in inputs.items():
                for block in self.blocks.values():
                    if block.name == block_name:
                        for port in block.outputs:
                            port.value = value
                        # Propagate to connected blocks
                        for output in block.outputs:
                            self.connector.propagate(output.id, value, self.blocks)
        
        # Get execution order
        execution_order = self.topological_sort()
        self._context.execution_order = execution_order
        
        try:
            # Execute blocks in order
            for block_id in execution_order:
                if self.state != FactoryState.RUNNING:
                    break
                
                block = self.blocks[block_id]
                self._context.iteration += 1
                
                # Skip if not ready
                if not block.is_ready():
                    continue
                
                # Execute block
                await self._execute_block(block)
                
                # Check timeout
                elapsed = time.time() - self._context.start_time
                if elapsed > self.config.timeout_seconds:
                    self.state = FactoryState.ERROR
                    self._context.errors.append("Execution timeout")
                    break
            
            if self.state == FactoryState.RUNNING:
                self.state = FactoryState.COMPLETED
            
        except Exception as e:
            self.state = FactoryState.ERROR
            self._context.errors.append(str(e))
            logger.exception("Factory execution error")
        
        # Collect outputs
        outputs = {}
        for block in self.get_output_blocks():
            for output in block.outputs:
                if output.value is not None:
                    outputs[f"{block.name}.{output.name}"] = output.value
        
        self._context.outputs = outputs
        return outputs
    
    async def _execute_block(self, block: Block) -> None:
        """Execute a single block."""
        block.state = "running"
        start_time = time.time()
        
        if self._context.on_block_start:
            self._context.on_block_start(block)
        
        try:
            result = await self._run_block_logic(block)
            
            block.state = "completed"
            block.execution_time = time.time() - start_time
            
            # Set output values and propagate
            if result is not None:
                for output in block.outputs:
                    if output.name == "result" or len(block.outputs) == 1:
                        output.value = result
                        self.connector.propagate(output.id, result, self.blocks)
            
            if self._context.on_block_complete:
                self._context.on_block_complete(block, result)
                
        except Exception as e:
            block.state = "error"
            block.error = str(e)
            block.execution_time = time.time() - start_time
            
            if self._context.on_block_error:
                self._context.on_block_error(block, e)
            
            if self.config.stop_on_error:
                raise
    
    async def _run_block_logic(self, block: Block) -> Any:
        """Execute the actual block logic based on type."""
        if block.block_type == BlockType.TOOL:
            return await self._execute_tool_block(block)
        elif block.block_type == BlockType.AGENT:
            return await self._execute_agent_block(block)
        elif block.block_type == BlockType.PROMPT:
            return self._execute_prompt_block(block)
        elif block.block_type == BlockType.ROUTER:
            return self._execute_router_block(block)
        elif block.block_type == BlockType.TRANSFORM:
            return self._execute_transform_block(block)
        elif block.block_type in (BlockType.INPUT, BlockType.OUTPUT):
            # Pass through
            if block.inputs:
                return block.inputs[0].value
            return None
        else:
            return None
    
    async def _execute_tool_block(self, block: Block) -> Any:
        """Execute a tool invocation block."""
        tool_name = block.params.get("tool_name", "")
        tool_params = dict(block.params.get("tool_params", {}))
        
        # Inject input values into params
        for inp in block.inputs:
            if inp.value is not None:
                tool_params[inp.name] = inp.value
        
        if not self._agent:
            return {"error": "No agent available for tool execution"}
        
        # Use Hermes tool handler
        from model_tools import handle_function_call
        
        result = handle_function_call(
            tool_name=tool_name,
            args=tool_params,
            task_id=f"factory_{self.id}_{block.id}",
        )
        
        return result
    
    async def _execute_agent_block(self, block: Block) -> Any:
        """Execute a sub-agent block."""
        system_prompt = block.params.get("system_prompt", "")
        model = block.params.get("model")
        tools = block.params.get("enabled_tools", [])
        
        prompt_input = None
        for inp in block.inputs:
            if inp.name == "prompt" and inp.value:
                prompt_input = inp.value
                break
        
        if not prompt_input:
            return {"error": "No prompt input provided"}
        
        if not self._agent:
            return {"error": "No parent agent available"}
        
        # Create sub-agent
        from run_agent import AIAgent
        
        sub_agent = AIAgent(
            model=model or self._agent.model,
            base_url=self._agent.base_url,
            api_key=self._agent._client_kwargs.get("api_key"),
            enabled_toolsets=tools if tools else None,
            max_iterations=20,
            quiet_mode=True,
        )
        
        response = sub_agent.run_conversation(
            user_message=prompt_input,
            system_message=system_prompt,
        )
        
        return response
    
    def _execute_prompt_block(self, block: Block) -> str:
        """Execute a prompt template block."""
        template = block.params.get("template", "")
        
        # Substitute variables
        result = template
        for inp in block.inputs:
            if inp.value is not None:
                result = result.replace(f"{{{inp.name}}}", str(inp.value))
        
        return result
    
    def _execute_router_block(self, block: Block) -> Any:
        """Execute a routing block."""
        conditions = block.params.get("conditions", [])
        data = block.inputs[0].value if block.inputs else None
        
        for cond in conditions:
            # Simple pattern matching for now
            pattern = cond.get("pattern", "")
            target = cond.get("target", "default")
            
            if pattern in str(data):
                return {"route": target, "data": data}
        
        return {"route": "default", "data": data}
    
    def _execute_transform_block(self, block: Block) -> Any:
        """Execute a data transformation block."""
        transform = block.params.get("transform", "")
        data = block.inputs[0].value if block.inputs else None
        
        if transform == "json_parse" and isinstance(data, str):
            import json
            return json.loads(data)
        elif transform == "json_stringify":
            import json
            return json.dumps(data)
        elif transform == "uppercase" and isinstance(data, str):
            return data.upper()
        elif transform == "lowercase" and isinstance(data, str):
            return data.lower()
        
        return data
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize factory to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "blocks": [b.to_dict() for b in self.blocks.values()],
            "connections": self.connector.to_list(),
            "config": {
                "max_iterations": self.config.max_iterations,
                "timeout_seconds": self.config.timeout_seconds,
                "parallel_execution": self.config.parallel_execution,
                "stop_on_error": self.config.stop_on_error,
            }
        }
    
    def to_json(self, indent: int = 2) -> str:
        """Serialize factory to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def save(self, path: Path) -> None:
        """Save factory to a JSON file."""
        path.write_text(self.to_json())
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Factory":
        """Deserialize factory from dictionary."""
        factory = cls(
            name=data.get("name", "Untitled"),
            description=data.get("description", ""),
        )
        factory.id = data.get("id", factory.id)
        
        # Load blocks
        for block_data in data.get("blocks", []):
            block = Block.from_dict(block_data)
            factory.blocks[block.id] = block
        
        # Load connections
        factory.connector.load_from_list(data.get("connections", []))
        
        # Load config
        config = data.get("config", {})
        factory.config.max_iterations = config.get("max_iterations", 100)
        factory.config.timeout_seconds = config.get("timeout_seconds", 300)
        factory.config.parallel_execution = config.get("parallel_execution", True)
        factory.config.stop_on_error = config.get("stop_on_error", False)
        
        return factory
    
    @classmethod
    def from_json(cls, json_str: str) -> "Factory":
        """Deserialize factory from JSON string."""
        return cls.from_dict(json.loads(json_str))
    
    @classmethod
    def load(cls, path: Path) -> "Factory":
        """Load factory from a JSON file."""
        return cls.from_json(path.read_text())
    
    def export_as_skill(self, output_dir: Path) -> Path:
        """
        Export the factory as a Hermes skill.
        
        Creates a SKILL.md file with the factory definition embedded.
        """
        skill_dir = output_dir / self.name.lower().replace(" ", "-")
        skill_dir.mkdir(parents=True, exist_ok=True)
        
        # Create SKILL.md
        skill_content = f"""---
name: {self.name.lower().replace(" ", "-")}
description: {self.description}
version: 1.0.0
metadata:
  hermes:
    type: factory
    tags: [factory, workflow]
---

# {self.name}

{self.description}

## Factory Definition

This skill is a visual factory workflow. Execute it using the `execute_factory` tool.

```json
{self.to_json()}
```

## Blocks

"""
        # Document each block
        for block in self.blocks.values():
            skill_content += f"### {block.name}\n"
            skill_content += f"- Type: {block.block_type.value}\n"
            if block.inputs:
                skill_content += f"- Inputs: {', '.join(p.name for p in block.inputs)}\n"
            if block.outputs:
                skill_content += f"- Outputs: {', '.join(p.name for p in block.outputs)}\n"
            skill_content += "\n"
        
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_content)
        
        return skill_path
