# Ralph MCP Server Design and Implementation Architecture

## Goal
- Make Ralph callable from GitHub Copilot Chat in JetBrains IDEs via MCP, with safe defaults and parity with the existing CLI behavior.

## Non-goals
- Replacing the CLI or terminal workflow.
- Bypassing user approval for tool execution.
- Expanding Ralph capabilities beyond what the CLI already supports.

## References
- GitHub Copilot plugin for JetBrains: https://plugins.jetbrains.com/plugin/17718-github-copilot
- Copilot Chat in JetBrains IDEs: https://docs.github.com/en/copilot/how-tos/chat-with-copilot/chat-in-ide?tool=jetbrains
- MCP in JetBrains IDEs: https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp/extend-copilot-chat-with-mcp?tool=jetbrains
- MCP support GA for JetBrains: https://github.blog/changelog/2025-08-13-model-context-protocol-mcp-support-for-jetbrains-eclipse-and-xcode-is-now-generally-available/
- MCP server build guide and best practices: https://modelcontextprotocol.io/docs/develop/build-server
- Code execution with MCP: https://www.anthropic.com/engineering/code-execution-with-mcp

## Constraints and assumptions from documentation
- MCP access can be gated by the "MCP servers in Copilot" policy for Copilot Business or Copilot Enterprise plans.
- JetBrains supports MCP servers configured from the MCP registry or via a local `mcp.json` file.
- JetBrains MCP configuration supports both local and remote servers.
- MCP support in JetBrains is generally available and requires the latest Copilot plugin plus a valid Copilot license.
- For stdio servers, do not write to stdout. Use stderr or file logging.
- If implemented in Python, MCP docs require Python 3.10+ and MCP SDK 1.2.0+.

## User experience
- Developer opens Copilot Chat in JetBrains, selects Agent mode, and invokes a Ralph tool.
- Copilot requests tool execution, developer approves, and Ralph runs in the configured workspace.
- Ralph returns a structured summary plus a link to a log or transcript file in the repo.

## Implementation architecture

### Module layout
- `ralph_py/mcp/`
  - `server.py`: MCP server instance and tool registration.
  - `schema.py`: Tool input models and validation helpers.
  - `runner.py`: Adapter layer that calls `init_cmd`, `run_loop`, and `understand`.
  - `resources.py`: Resource resolution for `ralph://` URIs.
  - `prompts.py`: Prompt templates that map to tool calls.
  - `logging.py`: Structured logging and transcript writing.
  - `transport/stdio.py`: stdio adapter setup.
  - `transport/http.py`: HTTP adapter setup.
- `ralph_py/mcp_server.py`
  - Standalone entry point for stdio or HTTP.

### Runtime modes
- Stdio mode for JetBrains `mcp.json` command execution.
- HTTP mode for remote or shared deployments.
- Both modes reuse the same server core and tool definitions.

### Entry point and flags
- `ralph mcp` (or `python -m ralph_py.mcp_server` for dev workflows)
- Flags:
  - `--transport` `stdio|http`
  - `--root` absolute path to repo
  - `--host` and `--port` for HTTP
  - `--log-dir` defaults to `./.ralph/logs`

### Core flow
1. MCP client sends a tool call request.
2. `server.py` validates input via `schema.py`.
3. `runner.py` builds a `RalphConfig` from env and explicit inputs.
4. Guardrails enforce `root` and `allowed_paths`.
5. `run_loop` or `init_cmd` executes with a bounded, single run context.
6. `logging.py` writes transcripts to `./.ralph/logs`.
7. Tool result returns a structured payload with summary and log path.

## Tool surface

### Tools
- `ralph.run`
  - Runs the main loop with explicit inputs.
  - Returns: summary, exit code, log path.
- `ralph.understand`
  - Runs the understanding loop with read only guardrails.
- `ralph.init`
  - Scaffolds `scripts/ralph` in the provided `root`.
- `ralph.validate`
  - Validates required files and returns actionable errors.

### Resources
- `ralph://prompt`
- `ralph://prd`
- `ralph://progress`
- `ralph://codebase_map`
- Map to `scripts/ralph` under the selected `root`.

### Prompts
- Built in prompts that map to tool calls:
  - "Run Ralph with the current prompt"
  - "Initialize Ralph scaffolding"
  - "Run understanding loop"

## Tool schemas and validation

### Shared inputs
- `root`: absolute path to the project root.
- `prompt_file`: path to prompt file, defaults to `scripts/ralph/prompt.md`.
- `prd_file`: path to PRD file, defaults to `scripts/ralph/prd.json`.
- `max_iterations`: non negative int.
- `sleep_seconds`: non negative float.
- `allowed_paths`: list of repo relative paths.
- `agent_cmd`: optional command string for custom agent.
- `model`: optional model id.
- `model_reasoning_effort`: optional reasoning setting.
- `ui_mode`: `auto` or `rich` or `plain`.
- `no_color`: bool.
- `ascii_only`: bool.
- `ai_raw`: bool.
- `ai_show_prompt`: bool.
- `ai_show_final`: bool.
- `ai_prompt_progress_every`: non negative int.
- `ai_tool_mode`: `summary` or `full` or `none`.
- `ai_sys_mode`: `summary` or `full`.

### Tool specific inputs
- `ralph.run`: shared inputs plus `allow_edits` flag, default false for MCP.
- `ralph.understand`: shared inputs plus `allow_edits` forced false.
- `ralph.init`: `root` plus optional `force` flag if we allow overwrites.
- `ralph.validate`: `root` plus optional `prompt_file` and `prd_file`.

### Validation rules
- Paths must resolve within `root`.
- `max_iterations` and `ai_prompt_progress_every` must be non negative.
- `allowed_paths` must be repo relative, no absolute paths.
- If `allow_edits` is false, reject any file changes after each iteration.

## Configuration

### JetBrains local `mcp.json` example
```json
{
  "servers": {
    "ralph": {
      "command": "ralph",
      "args": [
        "mcp",
        "--transport",
        "stdio",
        "--root",
        "/path/to/repo"
      ]
    }
  }
}
```

### Optional remote server example
```json
{
  "servers": {
    "ralph": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### Environment
- The MCP server should reuse `RalphConfig.from_env` to align with CLI behavior.
- Tool inputs should override environment values only when explicitly provided.
- Use absolute paths in `mcp.json` and for `--root` to avoid workspace ambiguity.

## Logging and transcripts
- Log root: `./.ralph/logs`.
- Log file naming: `YYYYMMDD-HHMMSS_toolname.log`.
- Store a compact JSON summary alongside each log file for client consumption.
- Never write logs to stdout in stdio mode.

## Concurrency and isolation
- Single flight per `root` to avoid overlapping runs that mutate the same repo.
- Use an in memory lock keyed by absolute root path.
- Allow concurrent runs only when different roots are used.

## Error handling
- Return a structured error payload with `code`, `message`, and `details`.
- Map validation errors to a stable `invalid_argument` code.
- Map runtime failures to `execution_failed` and include the log path.

## Security and safety
- Enforce `allowed_paths` and repo root constraints exactly as the CLI does.
- Default to read only operations unless a tool explicitly opts into edits.
- Require a clear output summary of which files changed and where logs are stored.

## Optional extension: code execution with MCP
- The Anthropic proposal describes clients that run code to call MCP tools, reducing context usage.
- This is a client side optimization and not required for JetBrains Copilot.
- If we decide to support it, add:
  - `ralph.search_tools` tool that returns tool names with optional detail level.
  - `ralph://tools` resource that exposes generated wrappers for tool calls.
  - A minimal file tree that mirrors tools for on demand discovery.
- Only implement this once we have a secure code execution environment.

## Testing strategy
- Unit tests for tool input validation and resource resolution.
- Integration tests with a fake agent and temp repo.
- Manual test in JetBrains: configure MCP, invoke tools, confirm outputs and logs.
- Use the MCP Inspector to validate tool schemas and responses before IDE testing.

## Decisions
- Transport: stdio and HTTP.
- Tool surface: full tools, resources, and prompts.
- Logs: `./.ralph/logs`.
- MCP entry point: `ralph mcp` (or `python -m ralph_py.mcp_server` for dev workflows).
- Default run mode: no edits unless explicitly allowed.
