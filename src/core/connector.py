"""
Connector - Manages connections between blocks.

Connections define the data flow in a factory, linking output ports
of one block to input ports of another.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import uuid

from .block import Block, BlockPort


@dataclass
class Connection:
    """
    A single connection between two block ports.
    
    Connections are directional: from an output port to an input port.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    # Source (output port)
    source_block_id: str = ""
    source_port_id: str = ""
    
    # Target (input port)
    target_block_id: str = ""
    target_port_id: str = ""
    
    # Visual routing (for curved connections)
    waypoints: List[Tuple[float, float]] = field(default_factory=list)
    
    # Connection state
    active: bool = False  # True when data is flowing
    last_value: Any = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize connection to dictionary."""
        return {
            "id": self.id,
            "source": {
                "block": self.source_block_id,
                "port": self.source_port_id,
            },
            "target": {
                "block": self.target_block_id,
                "port": self.target_port_id,
            },
            "waypoints": self.waypoints,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Connection":
        """Deserialize connection from dictionary."""
        source = data.get("source", {})
        target = data.get("target", {})
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            source_block_id=source.get("block", ""),
            source_port_id=source.get("port", ""),
            target_block_id=target.get("block", ""),
            target_port_id=target.get("port", ""),
            waypoints=data.get("waypoints", []),
        )


class Connector:
    """
    Manages all connections in a factory.
    
    Handles:
    - Creating and removing connections
    - Validating connection compatibility
    - Propagating data through connections
    - Detecting cycles
    """
    
    def __init__(self):
        self.connections: Dict[str, Connection] = {}
        
        # Index for fast lookup
        self._by_source: Dict[str, List[str]] = {}  # port_id -> [connection_id]
        self._by_target: Dict[str, List[str]] = {}  # port_id -> [connection_id]
    
    def connect(
        self,
        source_block: Block,
        source_port_name: str,
        target_block: Block,
        target_port_name: str,
    ) -> Optional[Connection]:
        """
        Create a connection between two blocks.
        
        Returns the connection if successful, None if invalid.
        """
        # Find ports
        source_port = source_block.get_output(source_port_name)
        target_port = target_block.get_input(target_port_name)
        
        if not source_port or not target_port:
            return None
        
        # Check if target port already has a connection
        if target_port.id in self._by_target:
            return None  # Single input rule
        
        # Check type compatibility
        if not self._types_compatible(source_port.data_type, target_port.data_type):
            return None
        
        # Create connection
        conn = Connection(
            source_block_id=source_block.id,
            source_port_id=source_port.id,
            target_block_id=target_block.id,
            target_port_id=target_port.id,
        )
        
        # Register
        self.connections[conn.id] = conn
        
        if source_port.id not in self._by_source:
            self._by_source[source_port.id] = []
        self._by_source[source_port.id].append(conn.id)
        
        if target_port.id not in self._by_target:
            self._by_target[target_port.id] = []
        self._by_target[target_port.id].append(conn.id)
        
        # Mark ports as connected
        source_port.connected = True
        target_port.connected = True
        
        return conn
    
    def disconnect(self, connection_id: str) -> bool:
        """Remove a connection."""
        if connection_id not in self.connections:
            return False
        
        conn = self.connections[connection_id]
        
        # Remove from indexes
        if conn.source_port_id in self._by_source:
            self._by_source[conn.source_port_id].remove(connection_id)
        if conn.target_port_id in self._by_target:
            self._by_target[conn.target_port_id].remove(connection_id)
        
        del self.connections[connection_id]
        return True
    
    def get_connections_from(self, port_id: str) -> List[Connection]:
        """Get all connections from a source port."""
        conn_ids = self._by_source.get(port_id, [])
        return [self.connections[cid] for cid in conn_ids if cid in self.connections]
    
    def get_connection_to(self, port_id: str) -> Optional[Connection]:
        """Get the connection to a target port (if any)."""
        conn_ids = self._by_target.get(port_id, [])
        if conn_ids and conn_ids[0] in self.connections:
            return self.connections[conn_ids[0]]
        return None
    
    def propagate(
        self,
        source_port_id: str,
        value: Any,
        blocks: Dict[str, Block],
    ) -> List[str]:
        """
        Propagate a value through connections from a source port.
        
        Returns list of target block IDs that received the value.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        updated_blocks = []
        conns = self.get_connections_from(source_port_id)
        logger.info(f"propagate({source_port_id}): found {len(conns)} connections, _by_source keys={list(self._by_source.keys())}")
        
        for conn in conns:
            conn.active = True
            conn.last_value = value
            
            target_block = blocks.get(conn.target_block_id)
            logger.info(f"  Looking for target_block_id={conn.target_block_id}, found={target_block is not None}, available_blocks={list(blocks.keys())}")
            if not target_block:
                continue
            
            # Find the target port and set its value
            # Match by port.id OR port.name since JSON may use either
            found_port = False
            for port in target_block.inputs:
                if port.id == conn.target_port_id or port.name == conn.target_port_id:
                    port.value = value
                    updated_blocks.append(target_block.id)
                    found_port = True
                    logger.info(f"    Set port {port.name} (id={port.id}) to {str(value)[:50]}")
                    break
            if not found_port:
                logger.info(f"    Port {conn.target_port_id} not found in block inputs: {[p.name for p in target_block.inputs]}")
        
        return updated_blocks
    
    def _types_compatible(self, source_type: str, target_type: str) -> bool:
        """Check if two port types are compatible."""
        # 'any' matches everything
        if source_type == "any" or target_type == "any":
            return True
        # Exact match
        if source_type == target_type:
            return True
        # String can connect to most things
        if source_type == "string" and target_type in ("text", "prompt"):
            return True
        return False
    
    def detect_cycle(self, blocks: Dict[str, Block]) -> Optional[List[str]]:
        """
        Detect cycles in the connection graph.
        
        Returns a list of block IDs forming the cycle, or None if no cycle.
        """
        visited = set()
        rec_stack = set()
        path = []
        
        def dfs(block_id: str) -> Optional[List[str]]:
            visited.add(block_id)
            rec_stack.add(block_id)
            path.append(block_id)
            
            block = blocks.get(block_id)
            if not block:
                path.pop()
                rec_stack.discard(block_id)
                return None
            
            # Find all blocks connected to this block's outputs
            for output in block.outputs:
                for conn in self.get_connections_from(output.id):
                    target_id = conn.target_block_id
                    
                    if target_id in rec_stack:
                        # Cycle found
                        cycle_start = path.index(target_id)
                        return path[cycle_start:]
                    
                    if target_id not in visited:
                        result = dfs(target_id)
                        if result:
                            return result
            
            path.pop()
            rec_stack.discard(block_id)
            return None
        
        for block_id in blocks:
            if block_id not in visited:
                result = dfs(block_id)
                if result:
                    return result
        
        return None
    
    def to_list(self) -> List[Dict[str, Any]]:
        """Serialize all connections to a list."""
        return [conn.to_dict() for conn in self.connections.values()]
    
    def load_from_list(self, data: List[Dict[str, Any]]) -> None:
        """Load connections from a serialized list."""
        import logging
        logger = logging.getLogger(__name__)
        
        self.connections.clear()
        self._by_source.clear()
        self._by_target.clear()
        
        logger.info(f"Loading {len(data)} connections")
        for conn_data in data:
            conn = Connection.from_dict(conn_data)
            logger.info(f"  Connection {conn.id}: {conn.source_block_id}.{conn.source_port_id} -> {conn.target_block_id}.{conn.target_port_id}")
            self.connections[conn.id] = conn
            
            if conn.source_port_id not in self._by_source:
                self._by_source[conn.source_port_id] = []
            self._by_source[conn.source_port_id].append(conn.id)
            
            if conn.target_port_id not in self._by_target:
                self._by_target[conn.target_port_id] = []
            self._by_target[conn.target_port_id].append(conn.id)
        
        logger.info(f"  _by_source keys: {list(self._by_source.keys())}")
