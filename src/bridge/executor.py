"""
Executor - Runs Shapez factories using Hermes Agent.

The executor takes a factory definition and executes it by:
1. Creating an AIAgent instance
2. Executing blocks in topological order
3. Routing tool calls through the agent
4. Collecting and returning results
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Add hermes-agent to path
_hermes_path = Path(__file__).resolve().parents[3] / "hermes-agent"
if _hermes_path.exists() and str(_hermes_path) not in sys.path:
    sys.path.insert(0, str(_hermes_path))


class FactoryExecutor:
    """
    Executes Shapez factories using Hermes Agent.
    
    The executor bridges the visual factory representation to actual
    agent execution, handling:
    - Tool block execution via Hermes tools
    - Agent block execution via sub-agents
    - Result propagation between blocks
    - Error handling and recovery
    """
    
    def __init__(
        self,
        model: str = None,
        api_key: str = None,
        base_url: str = None,
        max_iterations: int = 60,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.max_iterations = max_iterations
        
        self._agent = None
        self._observer = None
    
    def set_observer(self, observer) -> None:
        """Set an observer for execution events."""
        from .observer import AgentObserver
        if isinstance(observer, AgentObserver):
            self._observer = observer
    
    async def execute(
        self,
        factory,
        inputs: Dict[str, Any] = None,
        on_progress: Callable[[str, Any], None] = None,
    ) -> Dict[str, Any]:
        """
        Execute a factory.
        
        Args:
            factory: Factory instance to execute
            inputs: Initial input values
            on_progress: Callback for progress updates
        
        Returns:
            Dict of output values
        """
        from ..core.factory import Factory
        
        if not isinstance(factory, Factory):
            raise TypeError("Expected Factory instance")
        
        # Create agent if needed
        if not self._agent:
            self._agent = self._create_agent()
        
        # Set up callbacks for observer
        def on_block_start(block):
            if self._observer:
                self._observer.on_tool_start(
                    tool_name=f"block:{block.name}",
                    arguments=block.params,
                )
            if on_progress:
                on_progress("block_start", {"block": block.name})
        
        def on_block_complete(block, result):
            if self._observer:
                self._observer.on_tool_complete(
                    tool_name=f"block:{block.name}",
                    result=result,
                    duration=block.execution_time,
                )
            if on_progress:
                on_progress("block_complete", {
                    "block": block.name,
                    "result": result,
                    "duration": block.execution_time,
                })
        
        # Execute factory
        try:
            results = await factory.execute(
                inputs=inputs,
                agent=self._agent,
                on_block_start=on_block_start,
                on_block_complete=on_block_complete,
            )
            
            if on_progress:
                on_progress("factory_complete", {"results": results})
            
            return results
            
        except Exception as e:
            logger.exception("Factory execution failed")
            if on_progress:
                on_progress("factory_error", {"error": str(e)})
            raise
    
    def _create_agent(self):
        """Create a Hermes AIAgent instance."""
        try:
            from run_agent import AIAgent
            
            kwargs = {
                "max_iterations": self.max_iterations,
                "quiet_mode": True,
            }
            
            if self.model:
                kwargs["model"] = self.model
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            
            return AIAgent(**kwargs)
            
        except ImportError:
            logger.error("Could not import AIAgent from hermes-agent")
            raise RuntimeError(
                "Hermes Agent not found. Ensure hermes-agent is in the Python path."
            )
    
    async def execute_json(
        self,
        factory_json: str,
        inputs: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Execute a factory from JSON definition."""
        from ..core.factory import Factory
        
        factory = Factory.from_json(factory_json)
        return await self.execute(factory, inputs)
    
    async def execute_file(
        self,
        factory_path: Path,
        inputs: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Execute a factory from a JSON file."""
        from ..core.factory import Factory
        
        factory = Factory.load(factory_path)
        return await self.execute(factory, inputs)


# =============================================================================
# Tool integration for Hermes
# =============================================================================

def execute_factory_tool(
    factory_json: str,
    inputs: Optional[str] = None,
    task_id: str = None,
) -> str:
    """
    Hermes tool handler for executing factories.
    
    This function can be registered as a Hermes tool to allow the agent
    to execute factory workflows.
    
    Args:
        factory_json: JSON string defining the factory
        inputs: JSON string of input values
        task_id: Task ID for session isolation
    
    Returns:
        JSON string with results or error
    """
    import json
    
    try:
        input_dict = {}
        if inputs:
            input_dict = json.loads(inputs)
        
        executor = FactoryExecutor()
        
        # Run async execution
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                executor.execute_json(factory_json, input_dict)
            )
        finally:
            loop.close()
        
        return json.dumps({
            "success": True,
            "results": results,
        }, ensure_ascii=False)
        
    except json.JSONDecodeError as e:
        return json.dumps({
            "success": False,
            "error": f"Invalid JSON: {e}",
        })
    except Exception as e:
        logger.exception("Factory execution error")
        return json.dumps({
            "success": False,
            "error": str(e),
        })


# Tool schema for registration
EXECUTE_FACTORY_SCHEMA = {
    "name": "execute_factory",
    "description": (
        "Execute a Shapez factory workflow. Factories are visual workflows "
        "consisting of connected blocks that perform tool calls, agent "
        "operations, and data transformations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "factory_json": {
                "type": "string",
                "description": "JSON string defining the factory workflow",
            },
            "inputs": {
                "type": "string",
                "description": "JSON string of input values to inject into the factory",
            },
        },
        "required": ["factory_json"],
    },
}


def register_factory_tool():
    """Register the execute_factory tool with Hermes."""
    try:
        from tools.registry import registry
        
        registry.register(
            name="execute_factory",
            toolset="shapez",
            schema=EXECUTE_FACTORY_SCHEMA,
            handler=lambda args, **kw: execute_factory_tool(
                factory_json=args.get("factory_json", "{}"),
                inputs=args.get("inputs"),
                task_id=kw.get("task_id"),
            ),
            check_fn=lambda: True,  # Always available
        )
        logger.info("Registered execute_factory tool")
        
    except ImportError:
        logger.warning("Could not register factory tool - tools.registry not available")
