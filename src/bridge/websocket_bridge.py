"""
WebSocket Bridge - Real-time bidirectional communication between Shapez UI and Hermes Agent.

This bridge enables:
1. Live visualization of agent activity (tool calls, messages, state)
2. Remote factory execution from the UI
3. Live updates as factories execute
4. Session observation and replay

Usage:
    from bridge.websocket_bridge import WebSocketBridge
    
    bridge = WebSocketBridge(port=8765)
    bridge.start()
    
    # Connect observer to agent
    bridge.attach_to_agent(agent)
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Optional websockets import
try:
    import websockets
    from websockets.server import serve as ws_serve
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logger.warning("websockets not installed. WebSocket bridge will be disabled.")


class MessageType(str, Enum):
    """Types of WebSocket messages."""
    # Client -> Server
    SUBSCRIBE = "subscribe"           # Subscribe to agent session
    UNSUBSCRIBE = "unsubscribe"       # Unsubscribe from session
    EXECUTE_FACTORY = "execute_factory"  # Run a factory
    STOP_EXECUTION = "stop_execution"    # Stop current execution
    PING = "ping"                     # Keep-alive
    
    # Server -> Client
    AGENT_EVENT = "agent_event"       # Tool call, message, etc.
    FACTORY_UPDATE = "factory_update" # Factory execution progress
    SESSION_LIST = "session_list"     # Available sessions
    ERROR = "error"                   # Error message
    PONG = "pong"                     # Keep-alive response


@dataclass
class WebSocketMessage:
    """Standard message format for WebSocket communication."""
    type: str
    payload: Dict[str, Any]
    timestamp: float = None
    session_id: str = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
    
    def to_json(self) -> str:
        return json.dumps({
            "type": self.type,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
        })
    
    @classmethod
    def from_json(cls, data: str) -> "WebSocketMessage":
        obj = json.loads(data)
        msg_type = obj.get("type", "")
        
        # Handle both nested payload format and flat format
        # Nested: {"type": "x", "payload": {"key": "val"}}
        # Flat: {"type": "x", "key": "val"}
        if "payload" in obj:
            payload = obj.get("payload", {})
        else:
            # Flat format - everything except type/timestamp/session_id is payload
            payload = {k: v for k, v in obj.items() 
                      if k not in ("type", "timestamp", "session_id")}
        
        return cls(
            type=msg_type,
            payload=payload,
            timestamp=obj.get("timestamp", time.time()),
            session_id=obj.get("session_id"),
        )


class WebSocketBridge:
    """
    Bidirectional WebSocket bridge between Shapez UI and Hermes Agent.
    
    Features:
    - Multiple client connections
    - Session-based subscriptions
    - Live event streaming
    - Factory execution management
    """
    
    def __init__(self, host: str = "::", port: int = 8765):
        if not WEBSOCKETS_AVAILABLE:
            raise RuntimeError("websockets package required. Install with: pip install websockets")
        
        self.host = host
        self.port = port
        
        # Connected clients
        self._clients: Set[Any] = set()
        
        # Session subscriptions: session_id -> set of websockets
        self._subscriptions: Dict[str, Set[Any]] = {}
        
        # Active sessions being observed
        self._observed_sessions: Dict[str, Any] = {}  # session_id -> observer
        
        # Running factory executions
        self._executions: Dict[str, asyncio.Task] = {}
        
        # Event loop and server
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Agent reference (set via attach_to_agent)
        self._agent = None
        self._agent_callbacks_installed = False
    
    async def _handle_client(self, websocket):
        """Handle a client WebSocket connection."""
        self._clients.add(websocket)
        client_id = id(websocket)
        logger.info(f"Client connected: {client_id}")
        
        try:
            # Send initial session list
            await self._send_session_list(websocket)
            
            async for message in websocket:
                try:
                    logger.info(f"Received message: {message[:200]}...")
                    msg = WebSocketMessage.from_json(message)
                    logger.info(f"Parsed message type: {msg.type}")
                    await self._handle_message(websocket, msg)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON: {message[:100]}")
                    await self._send_error(websocket, "Invalid JSON")
                except Exception as e:
                    logger.exception(f"Error handling message: {e}")
                    await self._send_error(websocket, str(e))
        
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)
            # Remove from all subscriptions
            for session_id in list(self._subscriptions.keys()):
                self._subscriptions[session_id].discard(websocket)
            logger.info(f"Client disconnected: {client_id}")
    
    async def _handle_message(self, websocket, msg: WebSocketMessage):
        """Handle an incoming WebSocket message."""
        if msg.type == MessageType.PING:
            await websocket.send(WebSocketMessage(
                type=MessageType.PONG,
                payload={},
            ).to_json())
        
        elif msg.type == MessageType.SUBSCRIBE:
            session_id = msg.payload.get("session_id")
            if session_id:
                if session_id not in self._subscriptions:
                    self._subscriptions[session_id] = set()
                self._subscriptions[session_id].add(websocket)
                logger.info(f"Client subscribed to session: {session_id}")
        
        elif msg.type == MessageType.UNSUBSCRIBE:
            session_id = msg.payload.get("session_id")
            if session_id and session_id in self._subscriptions:
                self._subscriptions[session_id].discard(websocket)
        
        elif msg.type == MessageType.EXECUTE_FACTORY:
            logger.info(f"Received EXECUTE_FACTORY message")
            await self._execute_factory(websocket, msg.payload)
        
        elif msg.type == MessageType.STOP_EXECUTION:
            execution_id = msg.payload.get("execution_id")
            if execution_id and execution_id in self._executions:
                self._executions[execution_id].cancel()
        
        elif msg.type == "execute_tool":
            # Handle tool execution request from shapez mod
            await self._execute_tool(websocket, msg.payload)
        
        elif msg.type == "ai_request":
            # Handle AI request from shapez.io mod
            await self._handle_ai_request(websocket, msg.payload)
        
        elif msg.type == "task_request":
            # Handle task request from shapez.io mod (shape + color mode)
            await self._handle_task_request(websocket, msg.payload)
    
    async def _execute_factory(self, websocket, payload: Dict[str, Any]):
        """Execute a factory and stream updates."""
        logger.info(f"_execute_factory called with payload keys: {payload.keys()}")
        factory_json = payload.get("factory_json", "{}")
        inputs = payload.get("inputs", {})
        execution_id = f"exec_{time.time_ns()}"
        
        async def run():
            logger.info("run() coroutine started")
            try:
                from core.factory import Factory
                
                logger.info(f"Loading factory from JSON ({len(factory_json)} chars)")
                factory = Factory.from_json(factory_json)
                logger.info(f"Factory loaded: {factory.name}, {len(factory.blocks)} blocks")
                
                # Send start event
                logger.info("Sending factory started event")
                await self._broadcast_to_client(websocket, WebSocketMessage(
                    type=MessageType.FACTORY_UPDATE,
                    payload={
                        "execution_id": execution_id,
                        "status": "started",
                        "factory_name": factory.name,
                    },
                ))
                logger.info("Sent factory started event")
                
                # Execute with progress callbacks
                async def on_block_start(block):
                    await self._broadcast_to_client(websocket, WebSocketMessage(
                        type=MessageType.FACTORY_UPDATE,
                        payload={
                            "execution_id": execution_id,
                            "status": "block_started",
                            "block_id": block.id,
                            "block_name": block.name,
                            "block_type": block.block_type.value,
                        },
                    ))
                
                async def on_block_complete(block, result):
                    await self._broadcast_to_client(websocket, WebSocketMessage(
                        type=MessageType.FACTORY_UPDATE,
                        payload={
                            "execution_id": execution_id,
                            "status": "block_completed",
                            "block_id": block.id,
                            "block_name": block.name,
                            "result": str(result)[:2000] if result else None,
                            "execution_time": block.execution_time,
                        },
                    ))
                
                logger.info(f"Calling factory.execute with inputs: {inputs}")
                results = await factory.execute(
                    inputs=inputs,
                    agent=self._agent,
                    on_block_start=lambda b: asyncio.create_task(on_block_start(b)),
                    on_block_complete=lambda b, r: asyncio.create_task(on_block_complete(b, r)),
                )
                logger.info(f"Factory.execute completed with results: {results}")
                
                # Send completion
                await self._broadcast_to_client(websocket, WebSocketMessage(
                    type=MessageType.FACTORY_UPDATE,
                    payload={
                        "execution_id": execution_id,
                        "status": "completed",
                        "results": {k: str(v)[:1000] for k, v in results.items()},
                    },
                ))
                
            except asyncio.CancelledError:
                await self._broadcast_to_client(websocket, WebSocketMessage(
                    type=MessageType.FACTORY_UPDATE,
                    payload={
                        "execution_id": execution_id,
                        "status": "cancelled",
                    },
                ))
            except Exception as e:
                logger.exception(f"Factory execution error: {e}")
                await self._broadcast_to_client(websocket, WebSocketMessage(
                    type=MessageType.FACTORY_UPDATE,
                    payload={
                        "execution_id": execution_id,
                        "status": "error",
                        "error": str(e),
                    },
                ))
            finally:
                self._executions.pop(execution_id, None)
        
        logger.info(f"Creating task for execution {execution_id}")
        task = asyncio.create_task(run())
        self._executions[execution_id] = task
        logger.info(f"Task created: {task}")
    
    async def _execute_tool(self, websocket, payload: Dict[str, Any]):
        """Execute a single tool and return the result (for shapez mod)."""
        entity_id = payload.get("entity_id", "")
        tool_name = payload.get("tool_name", "")
        tool_params = payload.get("tool_params", {})
        
        logger.info(f"Executing tool '{tool_name}' for entity {entity_id}")
        
        try:
            from model_tools import handle_function_call
            
            result = handle_function_call(
                function_name=tool_name,
                function_args=tool_params,
                task_id=f"shapez_{entity_id}",
            )
            
            await websocket.send(WebSocketMessage(
                type="tool_result",
                payload={
                    "entity_id": entity_id,
                    "result": result,
                    "success": True,
                },
            ).to_json())
            
        except Exception as e:
            logger.exception(f"Tool execution error: {e}")
            await websocket.send(WebSocketMessage(
                type="tool_result",
                payload={
                    "entity_id": entity_id,
                    "result": json.dumps({"error": str(e)}),
                    "success": False,
                },
            ).to_json())
    
    async def _handle_ai_request(self, websocket, payload: Dict[str, Any]):
        """Handle AI request from shapez.io mod."""
        request_id = payload.get("request_id", "")
        provider = payload.get("provider", "gemini")
        prompt = payload.get("prompt", "")
        
        logger.info(f"AI request: provider={provider}, prompt={prompt[:50]}...")
        
        try:
            logger.info(f"Calling {provider} API...")
            if provider == "gemini":
                response = await self._call_gemini(prompt)
            elif provider == "anthropic":
                response = await self._call_anthropic(prompt)
            else:
                response = f"Unknown provider: {provider}"
            
            logger.info(f"Got response from {provider}: {response[:100]}...")
            
            reply = json.dumps({
                "type": "ai_response",
                "request_id": request_id,
                "provider": provider,
                "response": response,
            })
            logger.info(f"Sending reply: {reply[:100]}...")
            await websocket.send(reply)
            logger.info("Reply sent!")
            
        except Exception as e:
            logger.exception(f"AI request error: {e}")
            await websocket.send(json.dumps({
                "type": "error",
                "request_id": request_id,
                "message": str(e),
            }))
    
    # Singleton browser VM for web automation tasks
    _browser_vm = None
    _browser_vm_ready = False
    _browser_task_queue = []
    
    async def _handle_task_request(self, websocket, payload: Dict[str, Any]):
        """Handle task request from shapez.io mod with shape type and color mode."""
        request_id = payload.get("request_id", "")
        provider = payload.get("provider", "gemini")
        color_mode = payload.get("color_mode", "")
        task_type = payload.get("task_type", "circle")
        backend = payload.get("backend", "local")
        prompt = payload.get("prompt", "")
        
        logger.info(f"Task request: provider={provider}, color_mode={color_mode}, task_type={task_type}, backend={backend}")
        logger.info(f"Prompt: {prompt[:100]}...")
        
        try:
            # Notify task started
            await websocket.send(json.dumps({
                "type": "task_started",
                "request_id": request_id,
                "task_type": task_type,
                "backend": backend,
                "color_mode": color_mode,
            }))
            
            # Route based on task type AND provider
            if task_type == "rect":
                # Square = Browser Automation via Playwright in Vers VM
                response = await self._handle_browser_task(prompt, provider)
            elif task_type == "circle":
                # Circle = iMessage task (local)
                response = await self._handle_imessage_task(prompt, provider)
            elif task_type == "star":
                # Star = GitHub Admin via Apple Container
                response = await self._handle_github_task(prompt, provider)
            elif provider == "cloud_code":
                # Yellow mode - cloud code with pi agent
                response = await self._spawn_cloud_code_agent(prompt, task_type)
            elif provider == "gemini":
                response = await self._call_gemini(prompt)
            elif provider == "anthropic":
                response = await self._call_anthropic(prompt)
            else:
                response = f"Unknown task configuration: task_type={task_type}, provider={provider}"
            
            logger.info(f"Task response: {response[:100]}...")
            
            await websocket.send(json.dumps({
                "type": "task_response",
                "request_id": request_id,
                "task_type": task_type,
                "backend": backend,
                "provider": provider,
                "color_mode": color_mode,
                "response": response,
            }))
            
        except Exception as e:
            logger.exception(f"Task request error: {e}")
            await websocket.send(json.dumps({
                "type": "error",
                "request_id": request_id,
                "message": str(e),
            }))
    
    async def _handle_browser_task(self, prompt: str, provider: str) -> str:
        """Handle browser automation task by spawning a Hermes agent with browser tools.
        
        The agent has access to real browser tools that execute Playwright commands.
        """
        import asyncio
        import sys
        import os
        
        # Add hermes-agent to path
        hermes_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        if hermes_path not in sys.path:
            sys.path.insert(0, hermes_path)
        
        # Load environment variables
        from dotenv import load_dotenv
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
        
        try:
            from run_agent import AIAgent
            
            # Determine model and API key
            if provider == "anthropic":
                model = "anthropic/claude-sonnet-4-20250514"
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                base_url = "https://api.anthropic.com/v1"
            else:
                model = "google/gemini-2.0-flash-001"
                api_key = os.environ.get("OPENROUTER_API_KEY")
                base_url = "https://openrouter.ai/api/v1"
            
            if not api_key:
                return f"Error: API key not configured for {provider}"
            
            # Create a browser-focused agent using Playwright (Vers VM)
            agent = AIAgent(
                model=model,
                api_key=api_key,
                base_url=base_url,
                enabled_toolsets=["playwright"],  # Playwright via Vers VM
                max_iterations=10,
                quiet_mode=True,
            )
            
            # Browser-specific system instruction - FORCE tool usage
            browser_instruction = f"""You are a web browser automation agent. You MUST use the playwright_browser tool to get REAL, LIVE data from the web.

CRITICAL RULES:
1. You MUST call playwright_browser with action="navigate" and the URL
2. The tool returns the actual page content - read it carefully
3. DO NOT make up or guess content - only report what you actually see in the tool response
4. Your final answer MUST start with "OUTPUT: "

Example:
1. Call playwright_browser(action="navigate", url="https://news.ycombinator.com")
2. Read the returned content field
3. Reply with "OUTPUT: The top post is [actual title from the content]"

If the tool fails, say "OUTPUT: Failed to access the page"

DO NOT hallucinate. Only report what's in the playwright_browser response.

Task: {prompt}"""
            
            # Run the agent
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: agent.chat(browser_instruction)
            )
            
            # Handle None response
            if response is None:
                return "Browser task returned no response"
            
            # Extract just the OUTPUT part if present
            if "OUTPUT:" in response:
                output_start = response.index("OUTPUT:") + 7
                return response[output_start:].strip()
            
            return response
            
        except ImportError as e:
            logger.error(f"Failed to import AIAgent: {e}")
            return f"Browser agent unavailable: {e}"
        except Exception as e:
            logger.exception(f"Browser task error: {e}")
            return f"Browser task failed: {e}"
    
    async def _handle_imessage_task(self, prompt: str, provider: str) -> str:
        """Handle iMessage task by spawning a Hermes agent with terminal tools.
        
        Uses AppleScript via terminal to read iMessage data.
        """
        import asyncio
        import sys
        import os
        
        hermes_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        if hermes_path not in sys.path:
            sys.path.insert(0, hermes_path)
        
        from dotenv import load_dotenv
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
        
        try:
            from run_agent import AIAgent
            
            if provider == "anthropic":
                model = "anthropic/claude-sonnet-4-20250514"
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                base_url = "https://api.anthropic.com/v1"
            else:
                model = "google/gemini-2.0-flash-001"
                api_key = os.environ.get("OPENROUTER_API_KEY")
                base_url = "https://openrouter.ai/api/v1"
            
            if not api_key:
                return f"Error: API key not configured for {provider}"
            
            agent = AIAgent(
                model=model,
                api_key=api_key,
                base_url=base_url,
                enabled_toolsets=["terminal"],  # Terminal for AppleScript
                max_iterations=5,
                quiet_mode=True,
            )
            
            imessage_instruction = f"""You are an iMessage agent on macOS. You can read iMessage data using AppleScript via the terminal.

IMPORTANT: Your final response MUST start with "OUTPUT: " followed by the direct answer.

To read messages, use osascript commands like:
- osascript -e 'tell application "Messages" to get chats'
- Query the Messages SQLite database at ~/Library/Messages/chat.db

READ-ONLY: Do not send messages, only read existing data.

Task: {prompt}"""
            
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: agent.chat(imessage_instruction)
            )
            
            if response is None:
                return "iMessage task returned no response"
            
            if "OUTPUT:" in response:
                output_start = response.index("OUTPUT:") + 7
                return response[output_start:].strip()
            
            return response
            
        except Exception as e:
            logger.exception(f"iMessage task error: {e}")
            return f"iMessage task failed: {e}"
    
    async def _handle_github_task(self, prompt: str, provider: str) -> str:
        """Handle GitHub admin task by spawning a Hermes agent with terminal + web tools.
        
        Uses gh CLI or GitHub API via terminal.
        """
        import asyncio
        import sys
        import os
        
        hermes_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        if hermes_path not in sys.path:
            sys.path.insert(0, hermes_path)
        
        from dotenv import load_dotenv
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
        
        github_token = os.environ.get("GITHUB_API_KEY") or os.environ.get("GITHUB_TOKEN")
        
        try:
            from run_agent import AIAgent
            
            if provider == "anthropic":
                model = "anthropic/claude-sonnet-4-20250514"
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                base_url = "https://api.anthropic.com/v1"
            else:
                model = "google/gemini-2.0-flash-001"
                api_key = os.environ.get("OPENROUTER_API_KEY")
                base_url = "https://openrouter.ai/api/v1"
            
            if not api_key:
                return f"Error: API key not configured for {provider}"
            
            agent = AIAgent(
                model=model,
                api_key=api_key,
                base_url=base_url,
                enabled_toolsets=["terminal", "web"],  # Terminal for gh CLI, web for API
                max_iterations=10,
                quiet_mode=True,
            )
            
            github_instruction = f"""You are a GitHub administration agent.

{"✅ GITHUB_TOKEN is available in the environment." if github_token else "⚠️ GITHUB_TOKEN is NOT set - authenticate with 'gh auth login' first."}

Use the 'gh' CLI tool for GitHub operations:
- gh issue list/create/close
- gh pr list/view/merge
- gh repo view
- gh api for direct API calls

IMPORTANT: Your final response MUST start with "OUTPUT: " followed by the direct answer.

Task: {prompt}"""
            
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: agent.chat(github_instruction)
            )
            
            if response is None:
                return "GitHub task returned no response"
            
            if "OUTPUT:" in response:
                output_start = response.index("OUTPUT:") + 7
                return response[output_start:].strip()
            
            return response
            
        except Exception as e:
            logger.exception(f"GitHub task error: {e}")
            return f"GitHub task failed: {e}"
    
    async def _spawn_cloud_code_agent(self, prompt: str, task_type: str) -> str:
        """Spawn a Hermes agent with full coding capabilities for cloud code execution."""
        import asyncio
        import sys
        import os
        
        hermes_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        if hermes_path not in sys.path:
            sys.path.insert(0, hermes_path)
        
        from dotenv import load_dotenv
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
        
        try:
            from run_agent import AIAgent
            
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return "Error: ANTHROPIC_API_KEY not configured"
            
            # Use Claude for coding tasks
            agent = AIAgent(
                model="anthropic/claude-sonnet-4-20250514",
                api_key=api_key,
                base_url="https://api.anthropic.com/v1",
                enabled_toolsets=["terminal", "web", "code"],  # Full coding toolset
                max_iterations=15,
                quiet_mode=True,
            )
            
            cloud_code_instruction = f"""You are a cloud coding agent with full development capabilities.

You have access to:
- Terminal for running commands
- Web tools for research
- Code execution for testing

IMPORTANT: Your final response MUST start with "OUTPUT: " followed by the direct answer or result.

Task: {prompt}"""
            
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: agent.chat(cloud_code_instruction)
            )
            
            if response is None:
                return "Cloud code task returned no response"
            
            if "OUTPUT:" in response:
                output_start = response.index("OUTPUT:") + 7
                return response[output_start:].strip()
            
            return response
            
        except Exception as e:
            logger.exception(f"Cloud code task error: {e}")
            return f"Cloud code task failed: {e}"
    
    async def _call_gemini(self, prompt: str) -> str:
        """Call Gemini AI via the gemini_search tool or direct API."""
        import os
        
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return "Error: GOOGLE_API_KEY not set"
        
        try:
            import httpx
            
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    url,
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.7,
                            "maxOutputTokens": 1024,
                        }
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                # Extract text from response
                candidates = data.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if parts:
                        return parts[0].get("text", "No response text")
                
                return "No response from Gemini"
                
        except Exception as e:
            logger.exception(f"Gemini API error: {e}")
            return f"Gemini error: {str(e)}"
    
    async def _call_anthropic(self, prompt: str) -> str:
        """Call Anthropic Claude via direct API."""
        import os
        
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return "Error: ANTHROPIC_API_KEY not set"
        
        try:
            import httpx
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                # Extract text from response
                content = data.get("content", [])
                if content:
                    return content[0].get("text", "No response text")
                
                return "No response from Claude"
                
        except Exception as e:
            logger.exception(f"Anthropic API error: {e}")
            return f"Claude error: {str(e)}"
    
    async def _send_session_list(self, websocket):
        """Send list of available sessions to client."""
        sessions = []
        
        # Check hermes session files
        session_dir = Path.home() / ".hermes" / "sessions"
        if session_dir.exists():
            for f in sorted(session_dir.glob("*.json"), reverse=True)[:20]:
                try:
                    data = json.loads(f.read_text())
                    sessions.append({
                        "session_id": data.get("session_id", f.stem),
                        "model": data.get("model", "unknown"),
                        "message_count": len(data.get("messages", [])),
                        "filename": f.name,
                    })
                except Exception:
                    pass
        
        await websocket.send(WebSocketMessage(
            type=MessageType.SESSION_LIST,
            payload={"sessions": sessions},
        ).to_json())
    
    async def _send_error(self, websocket, error: str):
        """Send error message to client."""
        await websocket.send(WebSocketMessage(
            type=MessageType.ERROR,
            payload={"error": error},
        ).to_json())
    
    async def _broadcast_to_client(self, websocket, msg: WebSocketMessage):
        """Send message to a specific client."""
        try:
            await websocket.send(msg.to_json())
        except Exception:
            pass
    
    async def _broadcast_to_session(self, session_id: str, msg: WebSocketMessage):
        """Broadcast message to all clients subscribed to a session."""
        msg.session_id = session_id
        subscribers = self._subscriptions.get(session_id, set())
        
        for ws in list(subscribers):
            try:
                await ws.send(msg.to_json())
            except Exception:
                subscribers.discard(ws)
    
    async def broadcast_agent_event(
        self,
        session_id: str,
        event_type: str,
        data: Dict[str, Any],
    ):
        """Broadcast an agent event to subscribed clients."""
        await self._broadcast_to_session(session_id, WebSocketMessage(
            type=MessageType.AGENT_EVENT,
            payload={
                "event_type": event_type,
                **data,
            },
        ))
    
    def attach_to_agent(self, agent):
        """
        Attach this bridge to an AIAgent instance.
        
        Installs callbacks to capture tool calls, messages, and state changes.
        """
        self._agent = agent
        
        if self._agent_callbacks_installed:
            return
        
        # Get or create session ID
        session_id = getattr(agent, 'session_id', None) or f"session_{time.time_ns()}"
        
        # Install progress callback
        original_callback = getattr(agent, 'tool_progress_callback', None)
        
        def progress_callback(tool_name: str, args: dict, result: str = None):
            # Call original callback if exists
            if original_callback:
                original_callback(tool_name, args, result)
            
            # Broadcast to WebSocket clients
            if self._loop and self._running:
                asyncio.run_coroutine_threadsafe(
                    self.broadcast_agent_event(session_id, "tool_call", {
                        "tool_name": tool_name,
                        "arguments": args,
                        "result": result[:2000] if result else None,
                        "timestamp": time.time(),
                    }),
                    self._loop,
                )
        
        agent.tool_progress_callback = progress_callback
        self._agent_callbacks_installed = True
        logger.info(f"Attached WebSocket bridge to agent session: {session_id}")
    
    def start(self, blocking: bool = False):
        """
        Start the WebSocket server.
        
        Args:
            blocking: If True, run in current thread (blocking).
                     If False, run in background thread.
        """
        if blocking:
            asyncio.run(self._run_server())
        else:
            self._thread = threading.Thread(target=self._run_in_thread, daemon=True)
            self._thread.start()
    
    def _run_in_thread(self):
        """Run the server in a background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._run_server())
    
    async def _run_server(self):
        """Run the WebSocket server."""
        self._running = True
        logger.info(f"Starting WebSocket bridge on ws://{self.host}:{self.port}")
        
        async with ws_serve(self._handle_client, self.host, self.port):
            while self._running:
                await asyncio.sleep(1)
    
    def stop(self):
        """Stop the WebSocket server."""
        self._running = False
        
        # Cancel all executions
        for task in self._executions.values():
            task.cancel()
        
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
