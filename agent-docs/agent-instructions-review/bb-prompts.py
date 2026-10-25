import json, subprocess, sys
out=[]; cur=None
while True:
    cmd=["bb","prompt-history","list","--json","--limit","200"]
    if cur: cmd+=["--cursor",cur]
    d=json.loads(subprocess.run(cmd,capture_output=True,text=True,cwd="/tmp").stdout)
    out+=d["entries"]; cur=d.get("nextCursor")
    if not cur or not d["entries"]: break
json.dump(out,open("prompts.json","w"))
print(len(out))
