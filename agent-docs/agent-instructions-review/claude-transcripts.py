import json, os, glob, sys, re
out=[]
sess=[]
def text_of(c):
    if isinstance(c,str): return c
    if isinstance(c,list):
        parts=[]
        for p in c:
            if isinstance(p,dict) and p.get('type')=='text': parts.append(p.get('text',''))
            elif isinstance(p,dict) and p.get('type')=='tool_result': return None
        return '\n'.join(parts)
    return None
NOISE=re.compile(r'^\s*(<command-|<local-command|<system-reminder|Caveat:|\[Request interrupted|<task-notification|<bash-|This session is being continued|<user-prompt-submit)')
for f in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')):
    proj=os.path.basename(os.path.dirname(f))
    n_user=0; n_asst=0; first=None; last=None; side=False; agents=0; sidechain_file=False
    try:
        for line in open(f,errors='replace'):
            try: d=json.loads(line)
            except: continue
            ts=d.get('timestamp')
            if ts: first=first or ts; last=ts
            t=d.get('type')
            if t=='assistant':
                n_asst+=1
                for p in (d.get('message',{}).get('content') or []):
                    if isinstance(p,dict) and p.get('type')=='tool_use' and p.get('name') in ('Agent','Task','Workflow'): agents+=1
            if t!='user' or d.get('isSidechain') or d.get('isMeta'): continue
            if d.get('userType') and d.get('userType')!='external': continue
            txt=text_of(d.get('message',{}).get('content'))
            if not txt or NOISE.match(txt): continue
            if txt.startswith('<') and 'teammate' in txt[:200]: continue
            n_user+=1
            out.append(dict(src='claude',proj=proj,sess=os.path.basename(f)[:8],ts=ts,len=len(txt),text=txt))
    except Exception as e: print(e,file=sys.stderr)
    sess.append(dict(src='claude',proj=proj,file=f,first=first,last=last,n_user=n_user,n_asst=n_asst,agents=agents,size=os.path.getsize(f)))
for f in glob.glob(os.path.expanduser('~/.codex/sessions/**/*.jsonl'),recursive=True):
    n_user=0;first=None;last=None;cwd=None;orig=None
    for line in open(f,errors='replace'):
        try: d=json.loads(line)
        except: continue
        ts=d.get('timestamp'); 
        if ts: first=first or ts; last=ts
        p=d.get('payload',{}) or {}
        if d.get('type')=='session_meta':
            cwd=p.get('cwd'); orig=p.get('originator') or p.get('source')
        if d.get('type')=='event_msg' and p.get('type')=='user_message':
            txt=p.get('message','')
            if not txt: continue
            n_user+=1
            out.append(dict(src='codex',proj=cwd,sess=os.path.basename(f)[-20:-6],ts=ts,len=len(txt),text=txt,orig=str(orig)))
    sess.append(dict(src='codex',proj=cwd,file=f,first=first,last=last,n_user=n_user,orig=str(orig),size=os.path.getsize(f)))
json.dump(out,open(sys.argv[1]+'/msgs.json','w'))
json.dump(sess,open(sys.argv[1]+'/sess.json','w'))
print(len(out),len(sess))
