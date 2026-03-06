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
        return cls(
            type=obj.get("type", ""),
            payload=obj.get("payload", {}),
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
                    msg = WebSocketMessage.from_json(message)
                    await self._handle_message(websocket, msg)
                except json.JSONDecodeError:
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
