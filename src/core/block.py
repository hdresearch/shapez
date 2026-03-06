"""
Block - Base unit of the factory system.

Blocks represent discrete operations that can be connected together
to form agent workflows. Each block has input/output ports and
executes when all required inputs are satisfied.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import uuid


class BlockType(Enum):
    """Types of blocks available in the factory."""
    # Input/Output
    INPUT = "input"          # Entry point for data
    OUTPUT = "output"        # Exit point for results
    
    # Tool invocations
    TOOL = "tool"            # Calls a Hermes tool
    
    # Agent operations
    AGENT = "agent"          # Spawns a sub-agent
    PROMPT = "prompt"        # Text template with variables
    
    # Control flow
    ROUTER = "router"        # Conditional routing
    MERGE = "merge"          # Merge multiple inputs
    SPLIT = "split"          # Split to multiple outputs
    
    # Data operations
    TRANSFORM = "transform"  # Transform data shape
    FILTER = "filter"        # Filter based on predicate
    ACCUMULATE = "accumulate"  # Collect until threshold
    
    # Visualization only (not executed)
    OBSERVE = "observe"      # Display data without modifying


class PortDirection(Enum):
    """Direction of a block port."""
    INPUT = "input"
    OUTPUT = "output"


@dataclass
class BlockPort:
    """
    A connection point on a block.
    
    Ports are typed and can be connected to compatible ports
    on other blocks.
    """
    id: str
    name: str
    direction: PortDirection
    data_type: str = "any"  # Type hint for validation
    required: bool = True
    description: str = ""
    
    # Runtime state
    connected: bool = False
    value: Any = None


@dataclass
class BlockConfig:
    """Configuration for a block instance."""
    # Position in the UI
    x: float = 0.0
    y: float = 0.0
    
    # Visual properties
    width: float = 120.0
    height: float = 60.0
    color: str = "#4A90D9"
    
    # Collapsed state
    collapsed: bool = False


@dataclass
class Block:
    """
    A single block in the factory.
    
    Blocks are the fundamental unit of computation. Each block:
    - Has input and output ports
    - Contains configuration/parameters
    - Executes when all required inputs are ready
    - Produces output for connected blocks
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    block_type: BlockType = BlockType.TOOL
    
    # Ports
    inputs: List[BlockPort] = field(default_factory=list)
    outputs: List[BlockPort] = field(default_factory=list)
    
    # Block-specific parameters
    params: Dict[str, Any] = field(default_factory=dict)
    
    # Visual configuration
    config: BlockConfig = field(default_factory=BlockConfig)
    
    # Execution state
    state: str = "idle"  # idle, waiting, running, completed, error
    error: Optional[str] = None
    execution_time: float = 0.0
    
    def add_input(
        self,
        name: str,
        data_type: str = "any",
        required: bool = True,
        description: str = ""
    ) -> BlockPort:
        """Add an input port to the block."""
        port = BlockPort(
            id=f"{self.id}_in_{len(self.inputs)}",
            name=name,
            direction=PortDirection.INPUT,
            data_type=data_type,
            required=required,
            description=description,
        )
        self.inputs.append(port)
        return port
    
    def add_output(
        self,
        name: str,
        data_type: str = "any",
        description: str = ""
    ) -> BlockPort:
        """Add an output port to the block."""
        port = BlockPort(
            id=f"{self.id}_out_{len(self.outputs)}",
            name=name,
            direction=PortDirection.OUTPUT,
            data_type=data_type,
            required=False,
            description=description,
        )
        self.outputs.append(port)
        return port
    
    def get_input(self, name: str) -> Optional[BlockPort]:
        """Get input port by name."""
        for port in self.inputs:
            if port.name == name:
                return port
        return None
    
    def get_output(self, name: str) -> Optional[BlockPort]:
        """Get output port by name."""
        for port in self.outputs:
            if port.name == name:
                return port
        return None
    
    def is_ready(self) -> bool:
        """Check if all required inputs are satisfied."""
        for port in self.inputs:
            if port.required and port.value is None:
                return False
        return True
    
    def reset(self) -> None:
        """Reset block state for re-execution."""
        self.state = "idle"
        self.error = None
        self.execution_time = 0.0
        for port in self.inputs:
            port.value = None
        for port in self.outputs:
            port.value = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize block to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.block_type.value,
            "inputs": [
                {
                    "id": p.id,
                    "name": p.name,
                    "data_type": p.data_type,
                    "required": p.required,
                }
                for p in self.inputs
            ],
            "outputs": [
                {
                    "id": p.id,
                    "name": p.name,
                    "data_type": p.data_type,
                }
                for p in self.outputs
            ],
            "params": self.params,
            "config": {
                "x": self.config.x,
                "y": self.config.y,
                "width": self.config.width,
                "height": self.config.height,
                "color": self.config.color,
            }
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Block":
        """Deserialize block from dictionary."""
        block = cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data.get("name", ""),
            block_type=BlockType(data.get("type", "tool")),
            params=data.get("params", {}),
        )
        
        # Add ports
        for inp in data.get("inputs", []):
            port = block.add_input(
                name=inp.get("name", "input"),
                data_type=inp.get("data_type", "any"),
                required=inp.get("required", True),
            )
            # Preserve ID from JSON if provided
            if "id" in inp:
                port.id = inp["id"]
        
        for out in data.get("outputs", []):
            port = block.add_output(
                name=out.get("name", "output"),
                data_type=out.get("data_type", "any"),
            )
            # Preserve ID from JSON if provided  
            if "id" in out:
                port.id = out["id"]
        
        # Apply config
        config = data.get("config", {})
        block.config.x = config.get("x", 0)
        block.config.y = config.get("y", 0)
        block.config.width = config.get("width", 120)
        block.config.height = config.get("height", 60)
        block.config.color = config.get("color", "#4A90D9")
        
        return block


# Block factory functions for common block types

def create_tool_block(
    tool_name: str,
    params: Dict[str, Any] = None,
    name: str = None,
) -> Block:
    """Create a block that invokes a Hermes tool."""
    block = Block(
        name=name or tool_name,
        block_type=BlockType.TOOL,
        params={
            "tool_name": tool_name,
            "tool_params": params or {},
        },
    )
    block.add_input("input", data_type="any", required=False)
    block.add_output("result", data_type="any")
    block.add_output("error", data_type="string")
    block.config.color = "#4A90D9"  # Blue
    return block


def create_agent_block(
    system_prompt: str,
    model: str = None,
    tools: List[str] = None,
    name: str = "Sub-Agent",
) -> Block:
    """Create a block that spawns a sub-agent."""
    block = Block(
        name=name,
        block_type=BlockType.AGENT,
        params={
            "system_prompt": system_prompt,
            "model": model,
            "enabled_tools": tools or [],
        },
    )
    block.add_input("prompt", data_type="string")
    block.add_input("context", data_type="any", required=False)
    block.add_output("response", data_type="string")
    block.add_output("tool_results", data_type="any")
    block.config.color = "#9B59B6"  # Purple
    return block


def create_prompt_block(
    template: str,
    variables: List[str] = None,
    name: str = "Prompt",
) -> Block:
    """Create a block that formats a prompt template."""
    block = Block(
        name=name,
        block_type=BlockType.PROMPT,
        params={
            "template": template,
            "variables": variables or [],
        },
    )
    # Add input for each variable
    for var in (variables or []):
        block.add_input(var, data_type="string")
    block.add_output("formatted", data_type="string")
    block.config.color = "#27AE60"  # Green
    return block


def create_router_block(
    conditions: List[Dict[str, Any]] = None,
    name: str = "Router",
) -> Block:
    """Create a block that routes based on conditions."""
    block = Block(
        name=name,
        block_type=BlockType.ROUTER,
        params={
            "conditions": conditions or [],
        },
    )
    block.add_input("data", data_type="any")
    # Outputs are dynamically added based on conditions
    block.add_output("default", data_type="any")
    block.config.color = "#E67E22"  # Orange
    return block
