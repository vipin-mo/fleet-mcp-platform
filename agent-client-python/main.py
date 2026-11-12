import asyncio
import os
import json
import shutil
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from errors import describe_failure
from guardrails import check_feed_allows_writes, parse_feed_status, validate_ota_payload
from prompts import SYSTEM_PROMPT

# Load environment configurations. override=True: the key in this project's .env wins over any
# (possibly old) ANTHROPIC_API_KEY already exported in the shell. Without it, a stale shell key is used
# silently and every API call fails with an authentication error.
load_dotenv(override=True)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Where the current run stands, so a crash can be written to the ledger as an aborted session.
SESSION = {"started": False, "status": "not_started"}   # status: running | completed | aborted


def get_client():
    """Create the API client when it is needed, so a missing key gives a clear message, not an import error."""
    return Anthropic()


# Hard stop so a confused model can never loop (and spend money) forever.
# A run with two fix rounds took 12 turns, so 15 leaves room without removing the cap.
MAX_TURNS = 15

USER_PROMPT = (
    "Scan the active vehicle fleet and find vehicles with abnormal telemetry. For each one, run the "
    "statistical check, confirm the cause in the maintenance manual, apply the approved mitigation, "
    "and then verify that the mitigation worked."
)


def field_of(obj, *names, default=None):
    """Read a field that the MCP SDK spells differently across versions (snake_case in 2.x, camelCase in 1.x)."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def cleanup_workspace():
    """Removes temporary files and build artifacts to maintain system optimization."""
    print("Cleaning workspace temporary files...")
    temp_targets = ["__pycache__", ".pytest_cache", ".mcp_temp_cache"]
    for target in temp_targets:
        for root, dirs, _ in os.walk("."):
            for d in dirs:
                if d == target:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    for file in os.listdir("."):
        if file.endswith(".tmp") or file.startswith(".gdb_history"):
            try:
                os.remove(file)
            except OSError:
                pass


def log_agent_transaction(event_type: str, payload: dict):
    """Appends agent execution tracks directly into a structured daily JSONL ledger file."""
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file_path = os.path.join(log_dir, f"fleet_agent_ledger_{date_str}.jsonl")

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "payload": payload
    }
    with open(log_file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    if event_type == "SESSION_START":
        SESSION.update(started=True, status="running")
    elif event_type == "SESSION_COMPLETED":
        SESSION["status"] = "completed"
    elif event_type == "SESSION_ABORTED":
        SESSION["status"] = "aborted"


async def run_agent():
    cleanup_workspace()
    anthropic_client = get_client()

    # Define standard input-output pipe routing configurations for the Go server binary
    server_params = StdioServerParameters(
        command="./mcp-server-go/mcp-server-go-binary",
        args=[],
        env=os.environ.copy()
    )

    print("Connecting to Go MCP Server infrastructure via Stdio channels...")
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("Handshake complete. Connected successfully!")

            log_agent_transaction("SESSION_START", {"user_intent": USER_PROMPT, "model": CLAUDE_MODEL})

            # The tool list does not change during a run, so fetch it once.
            available_mcp_tools = await session.list_tools()
            anthropic_formatted_tools = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": field_of(tool, "input_schema", "inputSchema")
                }
                for tool in available_mcp_tools.tools
            ]

            messages = [{"role": "user", "content": USER_PROMPT}]
            total_input_tokens = 0
            total_output_tokens = 0
            # Latest feed_status seen from scan_fleet (None until the first scan).
            feed = {"status": None}

            for turn in range(1, MAX_TURNS + 1):
                print(f"\nConsulting Claude (turn {turn}/{MAX_TURNS})...")
                response = anthropic_client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=4000,
                    system=SYSTEM_PROMPT,
                    tools=anthropic_formatted_tools,
                    messages=messages
                )

                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                log_agent_transaction("MODEL_TURN", {
                    "turn": turn,
                    "stop_reason": response.stop_reason,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                })

                messages.append({"role": "assistant", "content": response.content})

                # Check if the agent has reached terminal conclusion
                if response.stop_reason == "end_turn":
                    text_outputs = [block.text for block in response.content if hasattr(block, 'text')]
                    final_answer = "\n".join(text_outputs)
                    print("\nFinal Context Output Matrix from Orchestrator:\n", final_answer)
                    log_agent_transaction("SESSION_COMPLETED", {
                        "turns": turn,
                        "total_input_tokens": total_input_tokens,
                        "total_output_tokens": total_output_tokens,
                        "final_summary": final_answer,
                    })
                    return

                if response.stop_reason != "tool_use":
                    # e.g. max_tokens or a refusal: stop instead of looping on a broken conversation.
                    print(f"Stopping: unexpected stop_reason '{response.stop_reason}'.")
                    log_agent_transaction("SESSION_ABORTED", {"reason": f"stop_reason={response.stop_reason}", "turns": turn})
                    return

                # Process parallel execution loops when tool steps are chosen
                tool_blocks = [b for b in response.content if getattr(b, 'type', None) == "tool_use"]
                # Writes in this turn are judged on the feed status from BEFORE the turn: the tools run in
                # parallel, so a scan in the same turn has not been seen yet.
                feed_at_turn_start = feed["status"]

                async def execute_single_tool(block):
                    tool_name = block.name
                    tool_args = block.input
                    tool_id = block.id

                    log_agent_transaction("TOOL_REQUESTED", {"tool": tool_name, "arguments": tool_args})

                    if tool_name == "send_ota_command":
                        is_safe, error_msg = check_feed_allows_writes(feed_at_turn_start)
                        if is_safe:
                            is_safe, error_msg = validate_ota_payload(tool_args)
                        if not is_safe:
                            print(f"[GUARDRAIL BLOCK] {error_msg}")
                            log_agent_transaction("GUARDRAIL_VIOLATION", {"tool": tool_name, "arguments": tool_args, "reason": error_msg})
                            return {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "is_error": True,
                                "content": json.dumps({"status": "rejected", "reason": error_msg})
                            }

                    is_error = False
                    try:
                        tool_result = await session.call_tool(tool_name, arguments=tool_args)
                        is_error = bool(field_of(tool_result, "is_error", "isError", default=False))
                        if hasattr(tool_result, 'content') and len(tool_result.content) > 0:
                            # Safe string property extraction using object fallbacks
                            content_block = tool_result.content[0]
                            result_text = getattr(content_block, 'text', str(content_block))
                        else:
                            result_text = "Success: Empty response content matrix returned."
                    except Exception as e:
                        is_error = True
                        result_text = f"Error executing tool payload: {str(e)}"

                    if tool_name == "scan_fleet":
                        # A failed or unreadable scan resets the status to None, which blocks writes.
                        feed["status"] = None if is_error else parse_feed_status(result_text)

                    log_agent_transaction("TOOL_RESPONSE", {"tool": tool_name, "result": result_text, "is_error": is_error})
                    block_result = {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_text
                    }
                    if is_error:
                        block_result["is_error"] = True
                    return block_result

                # Run all tool calls from this turn at the same time
                tool_results_content = await asyncio.gather(*[execute_single_tool(b) for b in tool_blocks])

                messages.append({
                    "role": "user",
                    "content": tool_results_content
                })

            print(f"Stopping: reached the {MAX_TURNS}-turn limit.")
            log_agent_transaction("SESSION_ABORTED", {"reason": "max_turns", "turns": MAX_TURNS})


def record_failure(exc):
    """Write a crash into the ledger so a failed run never looks like it never happened."""
    detail = describe_failure(exc)
    if SESSION["started"] and SESSION["status"] == "running":
        log_agent_transaction("SESSION_ABORTED", {"reason": "exception", "error": detail})
    elif not SESSION["started"]:
        log_agent_transaction("STARTUP_FAILED", {"error": detail})


def main():
    """Run the agent once. Exit codes: 0 finished, 2 stopped early (turn limit etc.), 1 crashed, 130 Ctrl+C."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Missing ANTHROPIC_API_KEY. Put it in .env (see .env.example) or export it, then try again.",
              file=sys.stderr)
        return 1
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        if SESSION["started"] and SESSION["status"] == "running":
            log_agent_transaction("SESSION_ABORTED", {"reason": "interrupted"})
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:   # includes ExceptionGroup from the MCP client
        print(f"\nERROR: {describe_failure(exc)}", file=sys.stderr)
        record_failure(exc)
        return 1
    return 0 if SESSION["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
