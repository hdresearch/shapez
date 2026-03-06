#!/usr/bin/env python3
"""
Shapez - Visual Agent Factory System

CLI entry point for the Shapez factory system.

Usage:
    shapez observe --session <session_id>  # Watch a live session
    shapez edit [factory.json]              # Open factory editor
    shapez run <factory.json> [--input key=value]  # Run a factory
    shapez export <factory.json> --output <dir>    # Export as skill
    shapez list                              # List available factories
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add source to path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))


def cmd_observe(args):
    """Watch a live Hermes session."""
    from src.bridge.observer import AgentObserver
    
    observer = AgentObserver()
    
    # Find session file
    hermes_home = Path.home() / ".hermes"
    session_dir = hermes_home / "sessions"
    
    if args.session:
        # Find matching session file
        session_file = None
        for f in session_dir.glob(f"session_{args.session}*.json"):
            session_file = f
            break
        if not session_file:
            print(f"Session not found: {args.session}")
            return 1
    else:
        # Find most recent session
        sessions = list(session_dir.glob("session_*.json"))
        if not sessions:
            print("No sessions found")
            return 1
        session_file = max(sessions, key=lambda p: p.stat().st_mtime)
    
    print(f"Observing: {session_file.name}")
    print("Press Ctrl+C to stop\n")
    
    def on_event(event):
        """Print events as they occur."""
        from src.bridge.observer import EventType, ToolEvent, MessageEvent
        
        if isinstance(event, ToolEvent):
            if event.event_type == EventType.TOOL_CALL:
                print(f"🔧 {event.tool_name}")
                if event.arguments:
                    args_preview = str(event.arguments)[:100]
                    print(f"   └─ {args_preview}")
            elif event.event_type == EventType.TOOL_RESULT:
                result_preview = str(event.result)[:200] if event.result else "(empty)"
                print(f"   ✓ {result_preview}")
            elif event.event_type == EventType.TOOL_ERROR:
                print(f"   ✗ Error: {event.error}")
        
        elif isinstance(event, MessageEvent):
            if event.event_type == EventType.USER_MESSAGE:
                print(f"\n👤 User: {event.content[:200]}")
            elif event.event_type == EventType.ASSISTANT_MESSAGE:
                print(f"\n🤖 Assistant: {event.content[:200]}")
    
    observer.add_listener(on_event)
    
    try:
        asyncio.run(observer.watch_session_file(session_file))
    except KeyboardInterrupt:
        print("\nStopped.")
    
    return 0


def cmd_run(args):
    """Run a factory."""
    from src.core.factory import Factory
    from src.bridge.executor import FactoryExecutor
    
    factory_path = Path(args.factory)
    if not factory_path.exists():
        print(f"Factory not found: {factory_path}")
        return 1
    
    # Parse inputs
    inputs = {}
    if args.input:
        for inp in args.input:
            if "=" in inp:
                key, value = inp.split("=", 1)
                inputs[key] = value
    
    print(f"Running: {factory_path.name}")
    
    def on_progress(event_type, data):
        if event_type == "block_start":
            print(f"  ▶ {data['block']}")
        elif event_type == "block_complete":
            duration = data.get('duration', 0)
            print(f"  ✓ {data['block']} ({duration:.2f}s)")
        elif event_type == "factory_complete":
            print("\n✓ Factory completed")
    
    executor = FactoryExecutor(
        model=args.model,
    )
    
    try:
        results = asyncio.run(
            executor.execute_file(factory_path, inputs, on_progress)
        )
        
        print("\nOutputs:")
        for key, value in results.items():
            print(f"  {key}: {str(value)[:200]}")
        
        return 0
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return 1


def cmd_export(args):
    """Export a factory as a Hermes skill."""
    from src.core.factory import Factory
    
    factory_path = Path(args.factory)
    if not factory_path.exists():
        print(f"Factory not found: {factory_path}")
        return 1
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    factory = Factory.load(factory_path)
    skill_path = factory.export_as_skill(output_dir)
    
    print(f"Exported to: {skill_path}")
    return 0


def cmd_list(args):
    """List available factories."""
    factories_dir = Path(__file__).parent / "factories"
    
    if not factories_dir.exists():
        print("No factories directory found")
        return 0
    
    factories = list(factories_dir.glob("*.json"))
    
    if not factories:
        print("No factories found")
        return 0
    
    print("Available factories:\n")
    for f in sorted(factories):
        try:
            data = json.loads(f.read_text())
            name = data.get("name", f.stem)
            desc = data.get("description", "")[:60]
            print(f"  {f.stem:20} - {desc}")
        except Exception:
            print(f"  {f.stem:20} - (invalid)")
    
    return 0


def cmd_create(args):
    """Create a new factory from a template."""
    from src.core.factory import Factory
    from src.core.block import create_tool_block, create_prompt_block
    
    factory = Factory(
        name=args.name,
        description=args.description or "",
    )
    
    # Add basic blocks
    if args.template == "tool":
        # Simple tool invocation template
        tool_block = create_tool_block(
            tool_name="terminal",
            params={"command": "echo 'Hello from factory!'"},
            name="Terminal Command",
        )
        factory.add_block(tool_block)
    
    elif args.template == "prompt":
        # Prompt template
        prompt_block = create_prompt_block(
            template="You are a helpful assistant. {instruction}",
            variables=["instruction"],
            name="System Prompt",
        )
        factory.add_block(prompt_block)
    
    # Save
    output_path = Path(args.output) if args.output else Path(f"{args.name}.json")
    factory.save(output_path)
    
    print(f"Created: {output_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Shapez - Visual Agent Factory System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # observe
    observe_parser = subparsers.add_parser("observe", help="Watch a live session")
    observe_parser.add_argument("--session", "-s", help="Session ID to watch")
    
    # run
    run_parser = subparsers.add_parser("run", help="Run a factory")
    run_parser.add_argument("factory", help="Factory JSON file")
    run_parser.add_argument("--input", "-i", action="append", help="Input key=value")
    run_parser.add_argument("--model", "-m", help="Override model")
    
    # export
    export_parser = subparsers.add_parser("export", help="Export as skill")
    export_parser.add_argument("factory", help="Factory JSON file")
    export_parser.add_argument("--output", "-o", required=True, help="Output directory")
    
    # list
    subparsers.add_parser("list", help="List available factories")
    
    # create
    create_parser = subparsers.add_parser("create", help="Create a new factory")
    create_parser.add_argument("name", help="Factory name")
    create_parser.add_argument("--description", "-d", help="Description")
    create_parser.add_argument("--template", "-t", default="tool", 
                               choices=["tool", "prompt", "agent"],
                               help="Template type")
    create_parser.add_argument("--output", "-o", help="Output file")
    
    args = parser.parse_args()
    
    if args.command == "observe":
        return cmd_observe(args)
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "export":
        return cmd_export(args)
    elif args.command == "list":
        return cmd_list(args)
    elif args.command == "create":
        return cmd_create(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
