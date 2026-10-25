import json,glob,os,sys,re,collections
out=[];sess=[]
SKIP=re.compile(r'^\s*(<environment_context|<user_instructions|# AGENTS\.md|<permissions|<turn_aborted|<skill|<collaboration_mode|<user_shell_command|<INSTRUCTIONS)')
for f in glob.glob(os.path.expanduser('~/.codex/sessions/**/*.jsonl'),recursive=True):
    cwd=None;orig=None;n=0;first=None
    for l in open(f,errors='replace'):
        try:d=json.loads(l)
        except: continue
        p=d.get('payload') or {}
        if d.get('type')=='session_meta': cwd=p.get('cwd');orig=p.get('originator'); first=p.get('timestamp') or d.get('timestamp')
        if d.get('type')=='response_item' and p.get('type')=='message' and p.get('role')=='user':
            txt='\n'.join(c.get('text','') for c in p.get('content',[]) if isinstance(c,dict))
            if not txt.strip() or SKIP.match(txt): continue
            n+=1; out.append(dict(src='codex',proj=cwd,orig=orig,sess=os.path.basename(f)[-42:-6],ts=d.get('timestamp'),len=len(txt),text=txt))
    sess.append(dict(file=f,proj=cwd,orig=orig,n_user=n,first=first))
json.dump(out,open(sys.argv[1]+'/codex_msgs.json','w'));json.dump(sess,open(sys.argv[1]+'/codex_sess.json','w'))
print(len(out)); print(collections.Counter(x['orig'] for x in out))
nu=sorted(x['n_user'] for x in sess); print('median',nu[len(nu)//2],'p90',nu[int(.9*len(nu))],'max',nu[-1])
# how many first msgs look agent-authored
