#!/usr/bin/env python3
"""
Shapez Web Server

A simple Flask server that serves the Shapez UI and provides an API
for factory execution.

Usage:
    python server.py [--port PORT] [--host HOST]
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='web')
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Factories directory
FACTORIES_DIR = Path(__file__).parent / "factories"


@app.route('/')
def index():
    """Serve the main UI."""
    return send_from_directory('web', 'index.html')


@app.route('/factories/<path:filename>')
def serve_factory(filename):
    """Serve factory JSON files."""
    return send_from_directory(FACTORIES_DIR, filename)


@app.route('/api/factories', methods=['GET'])
def list_factories():
    """List available factories."""
    factories = []
    if FACTORIES_DIR.exists():
        for f in FACTORIES_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                factories.append({
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "filename": f.name,
                    "blocks": len(data.get("blocks", [])),
                })
            except Exception as e:
                factories.append({
                    "name": f.stem,
                    "filename": f.name,
                    "error": str(e),
                })
    
    return jsonify({"factories": factories})


# Lazy-loaded AIAgent for factory execution
_hermes_agent = None


def _get_or_create_agent():
    """Get or create a Hermes AIAgent for factory execution."""
    global _hermes_agent
    if _hermes_agent is not None:
        return _hermes_agent
    
    # Try to import and create agent
    try:
        # Add hermes-agent to path if running from shapez submodule
        hermes_path = Path(__file__).parent.parent
        if (hermes_path / "run_agent.py").exists():
            sys.path.insert(0, str(hermes_path))
        
        from run_agent import AIAgent
        
        # Create agent with default settings
        _hermes_agent = AIAgent(
            max_iterations=20,
            quiet_mode=True,
        )
        logger.info("Created Hermes AIAgent for factory execution")
        return _hermes_agent
    except Exception as e:
        logger.warning(f"Could not create Hermes agent: {e}")
        return None


@app.route('/api/execute', methods=['POST'])
def execute_factory():
    """Execute a factory."""
    try:
        data = request.get_json()
        factory_json = data.get('factory_json', '{}')
        inputs = data.get('inputs', {})
        
        # Import and run factory
        from core.factory import Factory
        import asyncio
        
        factory = Factory.from_json(factory_json)
        
        # Get agent for execution (needed for agent/tool blocks)
        agent = _get_or_create_agent()
        
        # Run in event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(factory.execute(inputs=inputs, agent=agent))
        finally:
            loop.close()
        
        return jsonify({
            "success": True,
            "factory_name": factory.name,
            "results": results,
        })
        
    except Exception as e:
        logger.exception("Factory execution error")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@app.route('/api/save', methods=['POST'])
def save_factory():
    """Save a factory to disk."""
    try:
        data = request.get_json()
        factory_json = data.get('factory_json', '{}')
        filename = data.get('filename', 'untitled.json')
        
        # Ensure valid filename
        if not filename.endswith('.json'):
            filename += '.json'
        
        # Save to factories directory
        FACTORIES_DIR.mkdir(exist_ok=True)
        filepath = FACTORIES_DIR / filename
        
        # Validate JSON
        factory_data = json.loads(factory_json)
        
        with open(filepath, 'w') as f:
            json.dump(factory_data, f, indent=2)
        
        return jsonify({
            "success": True,
            "path": str(filepath),
        })
        
    except Exception as e:
        logger.exception("Save error")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@app.route('/api/observe/<session_id>', methods=['GET'])
def observe_session(session_id):
    """Get events from a session (Server-Sent Events would be better but this is simpler)."""
    try:
        from bridge.observer import AgentObserver
        
        # Find session file
        hermes_home = Path.home() / ".hermes" / "sessions"
        session_file = None
        
        for f in hermes_home.glob(f"session_{session_id}*.json"):
            session_file = f
            break
        
        if not session_file or not session_file.exists():
            return jsonify({"error": f"Session not found: {session_id}"}), 404
        
        # Load session data
        data = json.loads(session_file.read_text())
        messages = data.get("messages", [])
        
        # Convert to events
        events = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            
            if role == "tool":
                events.append({
                    "type": "tool_result",
                    "tool_name": msg.get("tool_name", ""),
                    "content": content[:500],
                })
            elif role == "user":
                events.append({
                    "type": "user_message",
                    "content": content[:500],
                })
            elif role == "assistant":
                events.append({
                    "type": "assistant_message",
                    "content": content[:500] if content else "",
                    "tool_calls": len(msg.get("tool_calls", [])),
                })
        
        return jsonify({
            "session_id": data.get("session_id"),
            "model": data.get("model"),
            "message_count": len(messages),
            "events": events[-50:],  # Last 50 events
        })
        
    except Exception as e:
        logger.exception("Observe error")
        return jsonify({"error": str(e)}), 500


# Global WebSocket bridge instance
_ws_bridge = None


def get_websocket_bridge():
    """Get the WebSocket bridge instance (lazy init)."""
    global _ws_bridge
    return _ws_bridge


@app.route('/api/ws-status', methods=['GET'])
def ws_status():
    """Check WebSocket bridge status."""
    bridge = get_websocket_bridge()
    if bridge and bridge._running:
        return jsonify({
            "status": "running",
            "port": bridge.port,
            "clients": len(bridge._clients),
        })
    return jsonify({"status": "not_running"})


def main():
    parser = argparse.ArgumentParser(description="Shapez Web Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--host", default="::", help="Host to bind to")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--no-websocket", action="store_true", help="Disable WebSocket bridge")
    args = parser.parse_args()
    
    print(f"🏭 Shapez Factory Server starting on http://{args.host}:{args.port}")
    print(f"   Factories directory: {FACTORIES_DIR}")
    
    # Start WebSocket bridge
    global _ws_bridge
    if not args.no_websocket:
        try:
            from bridge.websocket_bridge import WebSocketBridge, WEBSOCKETS_AVAILABLE
            if WEBSOCKETS_AVAILABLE:
                _ws_bridge = WebSocketBridge(host=args.host, port=args.ws_port)
                _ws_bridge.start(blocking=False)
                print(f"   WebSocket bridge on ws://{args.host}:{args.ws_port}")
            else:
                print("   WebSocket bridge disabled (websockets package not installed)")
        except Exception as e:
            print(f"   WebSocket bridge failed to start: {e}")
    
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
