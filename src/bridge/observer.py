"""
Observer - Watches Hermes Agent activity for visualization.

The observer attaches to a running agent session and emits events
that can be visualized in the Shapez UI.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of agent events."""
    # Messages
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    SYSTEM_MESSAGE = "system_message"
    
    # Tool operations
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    
    # Agent lifecycle
    AGENT_START = "agent_start"
    AGENT_STEP = "agent_step"
    AGENT_END = "agent_end"
    
    # Session events
    SESSION_START = "session_start"
    SESSION_RESET = "session_reset"
    
    # Context events
    CONTEXT_COMPRESS = "context_compress"
    
    # Sub-agent events
    DELEGATE_START = "delegate_start"
    DELEGATE_END = "delegate_end"


@dataclass
class ToolEvent:
    """Event for a tool invocation."""
    event_type: EventType
    timestamp: float
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None
    duration: float = 0.0
    
    # For nested tools (e.g., execute_code calling other tools)
    parent_event_id: Optional[str] = None


@dataclass
class MessageEvent:
    """Event for a message exchange."""
    event_type: EventType
    timestamp: float
    role: str
    content: str
    
    # For tool calls in assistant messages
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    
    # For tool responses
    tool_call_id: Optional[str] = None


@dataclass
class AgentEvent:
    """Generic agent lifecycle event."""
    event_type: EventType
    timestamp: float
    data: Dict[str, Any] = field(default_factory=dict)


class AgentObserver:
    """
    Observes Hermes Agent activity and emits visualization events.
    
    Can observe:
    - Live agent execution (via callbacks)
    - Session log files (for replay)
    - Gateway events (via hooks)
    """
    
    def __init__(self):
        self._listeners: List[Callable[[Any], None]] = []
        self._events: List[Any] = []
        self._watching: bool = False
        self._watch_task: Optional[asyncio.Task] = None
        
        # Event batching for performance
        self._batch_size = 10
        self._batch_interval = 0.1
        self._pending_events: List[Any] = []
    
    def add_listener(self, callback: Callable[[Any], None]) -> None:
        """Add a listener for events."""
        self._listeners.append(callback)
    
    def remove_listener(self, callback: Callable[[Any], None]) -> None:
        """Remove a listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)
    
    def emit(self, event: Any) -> None:
        """Emit an event to all listeners."""
        self._events.append(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Event listener error: {e}")
    
    def get_events(self) -> List[Any]:
        """Get all recorded events."""
        return self._events.copy()
    
    def clear_events(self) -> None:
        """Clear recorded events."""
        self._events.clear()
    
    # =========================================================================
    # Callback methods for AIAgent integration
    # =========================================================================
    
    def on_tool_start(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Called when a tool execution starts. Returns event ID."""
        event = ToolEvent(
            event_type=EventType.TOOL_CALL,
            timestamp=time.time(),
            tool_name=tool_name,
            arguments=arguments,
        )
        self.emit(event)
        return f"tool_{event.timestamp}"
    
    def on_tool_complete(
        self,
        tool_name: str,
        result: Any,
        duration: float,
        event_id: str = None,
    ) -> None:
        """Called when a tool execution completes."""
        event = ToolEvent(
            event_type=EventType.TOOL_RESULT,
            timestamp=time.time(),
            tool_name=tool_name,
            result=result,
            duration=duration,
        )
        self.emit(event)
    
    def on_tool_error(
        self,
        tool_name: str,
        error: str,
        event_id: str = None,
    ) -> None:
        """Called when a tool execution fails."""
        event = ToolEvent(
            event_type=EventType.TOOL_ERROR,
            timestamp=time.time(),
            tool_name=tool_name,
            error=error,
        )
        self.emit(event)
    
    def on_message(self, role: str, content: str, tool_calls: List = None) -> None:
        """Called when a message is sent or received."""
        if role == "user":
            event_type = EventType.USER_MESSAGE
        elif role == "assistant":
            event_type = EventType.ASSISTANT_MESSAGE
        else:
            event_type = EventType.SYSTEM_MESSAGE
        
        event = MessageEvent(
            event_type=event_type,
            timestamp=time.time(),
            role=role,
            content=content,
            tool_calls=tool_calls or [],
        )
        self.emit(event)
    
    def on_agent_start(self, session_id: str, model: str) -> None:
        """Called when agent starts processing."""
        event = AgentEvent(
            event_type=EventType.AGENT_START,
            timestamp=time.time(),
            data={
                "session_id": session_id,
                "model": model,
            },
        )
        self.emit(event)
    
    def on_agent_step(self, iteration: int, tool_count: int) -> None:
        """Called on each agent iteration."""
        event = AgentEvent(
            event_type=EventType.AGENT_STEP,
            timestamp=time.time(),
            data={
                "iteration": iteration,
                "tool_count": tool_count,
            },
        )
        self.emit(event)
    
    def on_agent_end(self, success: bool, message: str = None) -> None:
        """Called when agent finishes processing."""
        event = AgentEvent(
            event_type=EventType.AGENT_END,
            timestamp=time.time(),
            data={
                "success": success,
                "message": message,
            },
        )
        self.emit(event)
    
    def on_delegate_start(self, parent_id: str, child_id: str, prompt: str) -> None:
        """Called when a sub-agent is spawned."""
        event = AgentEvent(
            event_type=EventType.DELEGATE_START,
            timestamp=time.time(),
            data={
                "parent_id": parent_id,
                "child_id": child_id,
                "prompt": prompt[:200],  # Truncate for display
            },
        )
        self.emit(event)
    
    def on_delegate_end(self, child_id: str, success: bool) -> None:
        """Called when a sub-agent completes."""
        event = AgentEvent(
            event_type=EventType.DELEGATE_END,
            timestamp=time.time(),
            data={
                "child_id": child_id,
                "success": success,
            },
        )
        self.emit(event)
    
    # =========================================================================
    # Session file watching
    # =========================================================================
    
    async def watch_session_file(self, session_file: Path) -> None:
        """
        Watch a session file for changes and emit events.
        
        Useful for visualizing a live CLI session from another terminal.
        """
        self._watching = True
        last_modified = 0
        last_message_count = 0
        
        while self._watching:
            try:
                if session_file.exists():
                    mtime = session_file.stat().st_mtime
                    
                    if mtime > last_modified:
                        last_modified = mtime
                        
                        # Load session
                        data = json.loads(session_file.read_text())
                        messages = data.get("messages", [])
                        
                        # Emit new messages
                        for msg in messages[last_message_count:]:
                            role = msg.get("role", "")
                            content = msg.get("content", "")
                            tool_calls = msg.get("tool_calls", [])
                            
                            if role == "tool":
                                # Tool result
                                event = ToolEvent(
                                    event_type=EventType.TOOL_RESULT,
                                    timestamp=time.time(),
                                    tool_name=msg.get("tool_name", ""),
                                    result=content,
                                )
                                self.emit(event)
                            else:
                                self.on_message(role, content, tool_calls)
                        
                        last_message_count = len(messages)
                
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Session watch error: {e}")
                await asyncio.sleep(1)
        
        self._watching = False
    
    def start_watching(self, session_file: Path) -> None:
        """Start watching a session file in the background."""
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        
        self._watch_task = asyncio.create_task(
            self.watch_session_file(session_file)
        )
    
    def stop_watching(self) -> None:
        """Stop watching for changes."""
        self._watching = False
        if self._watch_task:
            self._watch_task.cancel()
    
    # =========================================================================
    # Replay from session file
    # =========================================================================
    
    def replay_session(
        self,
        session_file: Path,
        speed: float = 1.0,
        callback: Callable[[Any], None] = None,
    ) -> None:
        """
        Replay events from a session file.
        
        Args:
            session_file: Path to session JSON file
            speed: Playback speed multiplier (1.0 = real-time)
            callback: Optional callback for each event
        """
        data = json.loads(session_file.read_text())
        messages = data.get("messages", [])
        
        if callback:
            self.add_listener(callback)
        
        try:
            for i, msg in enumerate(messages):
                role = msg.get("role", "")
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", [])
                
                if role == "tool":
                    event = ToolEvent(
                        event_type=EventType.TOOL_RESULT,
                        timestamp=time.time(),
                        tool_name=msg.get("tool_name", ""),
                        result=content,
                    )
                    self.emit(event)
                else:
                    self.on_message(role, content, tool_calls)
                
                # Simulate timing
                if i < len(messages) - 1 and speed > 0:
                    time.sleep(0.1 / speed)
        finally:
            if callback:
                self.remove_listener(callback)


def create_observer_hooks(observer: AgentObserver) -> Dict[str, Callable]:
    """
    Create hook functions for Hermes Gateway integration.
    
    Returns a dict of hook functions that can be registered with
    the HookRegistry.
    """
    async def on_agent_start(event_type: str, context: Dict[str, Any]):
        observer.on_agent_start(
            session_id=context.get("session_id", ""),
            model=context.get("model", ""),
        )
    
    async def on_agent_step(event_type: str, context: Dict[str, Any]):
        observer.on_agent_step(
            iteration=context.get("iteration", 0),
            tool_count=context.get("tool_count", 0),
        )
    
    async def on_agent_end(event_type: str, context: Dict[str, Any]):
        observer.on_agent_end(
            success=context.get("success", True),
            message=context.get("message"),
        )
    
    return {
        "agent:start": on_agent_start,
        "agent:step": on_agent_step,
        "agent:end": on_agent_end,
    }
