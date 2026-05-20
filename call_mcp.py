import json
import subprocess
import sys

def call_mcp_tool(script_path, method, params):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    
    proc = subprocess.Popen(
        [sys.executable, script_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    stdout, stderr = proc.communicate(input=header + body)
    
    if stderr:
        print(f"Stderr: {stderr.decode()}", file=sys.stderr)
    
    # The output also has headers
    if stdout:
        # Simple parser for the response with headers
        parts = stdout.split(b"\r\n\r\n", 1)
        if len(parts) == 2:
            print(parts[1].decode())
        else:
            print(stdout.decode())

if __name__ == "__main__":
    call_mcp_tool("mcp/gemini_codex_mcp.py", "tools/call", {"name": "workspace_snapshot", "arguments": {}})
