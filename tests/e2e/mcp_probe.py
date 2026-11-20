"""Read-only inspection of the final mock forms through the pinned browser MCP."""

import json
import queue
import re
import subprocess
import threading


def inspect_forms(ws, port):
    command = ["npx", "--prefer-offline", "-y", "chrome-devtools-mcp@1.10.1",
               f"--browser-url=http://127.0.0.1:{port}",
               "--no-usage-statistics", f"--workspace={ws}"]
    lines = queue.Queue()
    with (ws / "probe-stderr.log").open("w") as err:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, text=True)
        def read_lines():
            for line in proc.stdout:
                lines.put(line)

        threading.Thread(target=read_lines, daemon=True).start()
        sequence = 0

        def call(method, params):
            nonlocal sequence
            sequence += 1
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": sequence,
                                         "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            while True:
                response = json.loads(lines.get(timeout=60))
                if response.get("id") == sequence:
                    if "error" in response:
                        raise RuntimeError(response["error"])
                    return response["result"]

        def tool(name, arguments):
            result = call("tools/call", {"name": name, "arguments": arguments})
            if result.get("isError"):
                raise RuntimeError(result)
            return "\n".join(c.get("text", "") for c in result.get("content", []))

        try:
            call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "job-agent-readonly-probe", "version": "1"}})
            proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()
            pages = tool("list_pages", {})
            forms = []
            for page_id, url in re.findall(r"^\s*(\d+):.*?\((https?://\S+)\)", pages, re.M):
                if not re.match(r"http://127\.0\.0\.1:\d+/(gh|wd)/", url) or "/thanks" in url:
                    continue
                snapshot = tool("take_snapshot", {"pageId": int(page_id)})
                if url.split("#")[0] not in snapshot:
                    raise RuntimeError("page URL changed before read-only probe")
                text = tool("evaluate_script", {"pageId": int(page_id), "function": """() => ({
                    url: location.href,
                    fields: Object.fromEntries([...document.querySelectorAll('input[name],textarea[name]')]
                      .filter(e => e.type !== 'file').map(e => [e.name,e.type === 'checkbox' ? e.checked : e.value])),
                    trusted: typeof trusted === 'undefined' ? [] : [...trusted],
                    resume: typeof resumeData === 'undefined' || !resumeData ? null :
                      {name:resumeData.name,size:resumeData.size},
                    review: Object.fromEntries([...document.querySelectorAll('#review tr')]
                      .map(row => [...row.querySelectorAll('td')].map(e => e.textContent))),
                    body: document.body.innerText
                })"""})
                match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                if not match:
                    raise RuntimeError("MCP probe did not return JSON")
                forms.append(json.loads(match.group(1)))
            return forms
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdin.close()
            proc.stdout.close()
