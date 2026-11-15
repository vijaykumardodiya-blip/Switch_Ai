#!/usr/bin/env python3
"""Switchboard by Vijay Dodiya. Deployable single-owner AI workspace."""
import os, json, time, sqlite3, secrets, hashlib, hmac
from pathlib import Path
from functools import wraps
from urllib.parse import quote
import urllib.request, urllib.error
from flask import Flask, request, jsonify, session, send_from_directory

ROOT=Path(__file__).resolve().parent
# Read a local .env file without requiring python-dotenv. Real environment values win.
env_file=ROOT/'.env'
if env_file.exists():
 for line in env_file.read_text(encoding='utf-8-sig').splitlines():
  line=line.strip()
  if line and not line.startswith('#') and '=' in line:
   k,v=line.split('=',1); os.environ.setdefault(k.strip(),v.strip().strip('\"').strip("'"))
DATA=Path(os.environ.get('DATA_DIR', str(ROOT/'data'))); DATA.mkdir(parents=True,exist_ok=True)
UPLOADS=DATA/'uploads'; UPLOADS.mkdir(exist_ok=True)
DB=DATA/'switchboard.sqlite3'
app=Flask(__name__, static_folder=None)
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'
app.config['SESSION_COOKIE_SECURE']=os.environ.get('COOKIE_SECURE','false').lower()=='true'
app.config['MAX_CONTENT_LENGTH']=15*1024*1024
APP_PASSWORD=os.environ.get('APP_PASSWORD','')
PORT=int(os.environ.get('PORT','8765'))
PROVIDERS={
 'openai':{'label':'ChatGPT','key':'OPENAI_API_KEY','model':'OPENAI_MODEL','default':'gpt-5.5','suggestions':['gpt-5.5','gpt-5-mini','gpt-4.1']},
 'anthropic':{'label':'Claude','key':'ANTHROPIC_API_KEY','model':'ANTHROPIC_MODEL','default':'claude-sonnet-5-5','suggestions':['claude-sonnet-5-5','claude-opus-5-5','claude-haiku-5-5']},
 'gemini':{'label':'Gemini','key':'GEMINI_API_KEY','model':'GEMINI_MODEL','default':'gemini-2.5-flash','suggestions':['gemini-2.5-flash','gemini-2.5-flash-lite']},
 'openrouter':{'label':'OpenRouter','key':'OPENROUTER_API_KEY','model':'OPENROUTER_MODEL','default':'openrouter/free','suggestions':['openrouter/free']}}
def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init_db():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,title TEXT NOT NULL,created REAL,updated REAL)')
  c.execute('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT,role TEXT,content TEXT,provider TEXT,model TEXT,created REAL,FOREIGN KEY(project_id) REFERENCES projects(id))')
  c.execute('CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,filename TEXT NOT NULL,stored_name TEXT NOT NULL,created REAL,FOREIGN KEY(project_id) REFERENCES projects(id))')
  if not c.execute('SELECT 1 FROM projects LIMIT 1').fetchone():
   pid=secrets.token_urlsafe(9); c.execute('INSERT INTO projects VALUES(?,?,?,?)',(pid,'My first project',time.time(),time.time()))
init_db()
def require_login(fn):
 @wraps(fn)
 def wrapped(*a,**kw):
  if not session.get('logged_in'): return jsonify(error='Please sign in first.'),401
  return fn(*a,**kw)
 return wrapped
def provider_key(pid): return os.environ.get(PROVIDERS[pid]['key'],'').strip()
def post_json(url,headers,payload):
 req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',**headers},method='POST')
 try:
  with urllib.request.urlopen(req,timeout=180) as r: return json.loads(r.read().decode())
 except urllib.error.HTTPError as e:
  try: msg=json.loads(e.read().decode()).get('error',{}).get('message',str(e))
  except: msg=str(e)
  raise ValueError(str(msg))
def model_call(pid,model,system,messages):
 key=provider_key(pid)
 if not key: raise ValueError('API key is not configured on the server for '+PROVIDERS[pid]['label'])
 if pid in ('openai','openrouter'):
  base='https://api.openai.com/v1' if pid=='openai' else 'https://openrouter.ai/api/v1'
  msgs=([{'role':'system','content':system}] if system else [])+messages
  d=post_json(base+'/chat/completions',{'Authorization':'Bearer '+key},{'model':model,'messages':msgs})
  return d['choices'][0]['message']['content']
 if pid=='anthropic':
  d=post_json('https://api.anthropic.com/v1/messages',{'x-api-key':key,'anthropic-version':'2023-06-01'},{'model':model,'max_tokens':8192,'system':system,'messages':messages})
  return ''.join(x.get('text','') for x in d.get('content',[]) if x.get('type')=='text')
 model=model.removeprefix('models/')
 contents=[{'role':'model' if x['role']=='assistant' else 'user','parts':[{'text':x['content']}]} for x in messages]
 payload={'contents':contents}
 if system: payload['systemInstruction']={'parts':[{'text':system}]}
 d=post_json('https://generativelanguage.googleapis.com/v1beta/models/'+quote(model,safe='')+':generateContent',{'x-goog-api-key':key},payload)
 return ''.join(x.get('text','') for x in d['candidates'][0]['content']['parts'] if not x.get('thought'))
@app.get('/')
def home(): return send_from_directory(ROOT,'index.html')
@app.post('/api/login')
def login():
 if not APP_PASSWORD: return jsonify(error='Owner password is not configured. Set APP_PASSWORD on the server.'),503
 data=request.get_json(silent=True) or {}
 if not hmac.compare_digest(str(data.get('password','')),APP_PASSWORD): return jsonify(error='Incorrect password.'),403
 session.clear(); session['logged_in']=True; return jsonify(ok=True)
@app.post('/api/logout')
@require_login
def logout(): session.clear(); return jsonify(ok=True)
@app.get('/api/session')
def session_status(): return jsonify(logged_in=bool(session.get('logged_in')))
@app.get('/api/providers')
@require_login
def providers():
 return jsonify(providers=[{'id':k,'label':v['label'],'configured':bool(provider_key(k)),'default_model':os.environ.get(v['model'],'').strip() or v['default'],'suggestions':v['suggestions']} for k,v in PROVIDERS.items()])
@app.get('/api/projects')
@require_login
def projects():
 with db() as c: rows=c.execute('SELECT id,title,updated FROM projects ORDER BY updated DESC').fetchall()
 return jsonify(projects=[dict(x) for x in rows])
@app.post('/api/projects')
@require_login
def new_project():
 d=request.get_json(silent=True) or {}; title=str(d.get('title') or 'New project')[:100]; pid=secrets.token_urlsafe(9); now=time.time()
 with db() as c: c.execute('INSERT INTO projects VALUES(?,?,?,?)',(pid,title,now,now))
 return jsonify(id=pid,title=title)
@app.get('/api/projects/<pid>')
@require_login
def get_project(pid):
 with db() as c:
  if not c.execute('SELECT id FROM projects WHERE id=?',(pid,)).fetchone(): return jsonify(error='Project not found'),404
  rows=c.execute('SELECT role,content,provider,model,created FROM messages WHERE project_id=? ORDER BY id',(pid,)).fetchall()
 return jsonify(messages=[dict(x) for x in rows])
@app.post('/api/upload')
@require_login
def upload():
 f=request.files.get('file'); pid=request.form.get('project_id','')
 if not f: return jsonify(error='Choose a file first.'),400
 if not pid: return jsonify(error='Open a project before uploading.'),400
 with db() as c:
  if not c.execute('SELECT id FROM projects WHERE id=?',(pid,)).fetchone(): return jsonify(error='Project not found.'),404
 name=Path(f.filename or 'attachment').name[:150]; ext=Path(name).suffix.lower(); raw=f.read()
 if not raw: return jsonify(error='The selected file is empty.'),400
 if len(raw)>15*1024*1024: return jsonify(error='Maximum file size is 15 MB.'),413
 safe=secrets.token_hex(12)+ext; (UPLOADS/safe).write_bytes(raw); aid=secrets.token_urlsafe(12)
 extracted=''
 if ext in ('.txt','.md','.csv','.json','.xml','.html','.htm','.log','.py','.js','.css'):
  extracted=raw.decode('utf-8','replace')[:120000]
 elif ext=='.pdf':
  try:
   from pypdf import PdfReader
   import io
   extracted='\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(raw)).pages)[:120000]
  except Exception as e: extracted='PDF saved, but text extraction failed: '+str(e)[:250]
 elif ext=='.docx':
  try:
   from docx import Document
   import io
   extracted='\n'.join(paragraph.text for paragraph in Document(io.BytesIO(raw)).paragraphs)[:120000]
  except Exception as e: extracted='DOCX saved, but text extraction failed: '+str(e)[:250]
 with db() as c: c.execute('INSERT INTO attachments VALUES(?,?,?,?,?)',(aid,pid,name,safe,time.time()))
 return jsonify(id=aid,name=name,stored=safe,text=extracted,notice=('Text extracted and ready for the next message.' if extracted else 'File saved with this project. Its contents are not text-extracted in this version.'))

@app.get('/api/projects/<pid>/attachments')
@require_login
def project_attachments(pid):
 with db() as c:
  rows=c.execute('SELECT id,filename,created FROM attachments WHERE project_id=? ORDER BY created DESC',(pid,)).fetchall()
 return jsonify(attachments=[dict(x) for x in rows])

@app.get('/api/attachments/<aid>/download')
@require_login
def download_attachment(aid):
 with db() as c: row=c.execute('SELECT filename,stored_name FROM attachments WHERE id=?',(aid,)).fetchone()
 if not row: return jsonify(error='Attachment not found.'),404
 return send_from_directory(UPLOADS,row['stored_name'],as_attachment=True,download_name=row['filename'])

@app.post('/api/chat')
@require_login
def chat():
 d=request.get_json(silent=True) or {}; pid=d.get('project_id'); provider=d.get('provider')
 if provider not in PROVIDERS: return jsonify(error='Unknown provider.'),400
 user_text=str(d.get('message','')).strip(); attachment=d.get('attachment') or {}
 if attachment.get('text'): user_text += '\n\n[Attached file: '+str(attachment.get('name','file'))+']\n'+str(attachment['text'])
 if not user_text: return jsonify(error='Write a message or attach a file.'),400
 with db() as c:
  if not c.execute('SELECT id FROM projects WHERE id=?',(pid,)).fetchone(): return jsonify(error='Select a valid project.'),404
  c.execute('INSERT INTO messages(project_id,role,content,provider,model,created) VALUES(?,?,?,?,?,?)',(pid,'user',user_text,provider,'',time.time()))
  rows=c.execute('SELECT role,content FROM messages WHERE project_id=? ORDER BY id',(pid,)).fetchall()
 model=str(d.get('model') or os.environ.get(PROVIDERS[provider]['model'],'') or PROVIDERS[provider]['default'])
 try: answer=model_call(provider,model,str(d.get('system','')), [dict(x) for x in rows])
 except Exception as e: return jsonify(error=str(e)),502
 with db() as c:
  c.execute('INSERT INTO messages(project_id,role,content,provider,model,created) VALUES(?,?,?,?,?,?)',(pid,'assistant',answer,provider,model,time.time()))
  c.execute('UPDATE projects SET updated=? WHERE id=?',(time.time(),pid))
 return jsonify(text=answer,model=model,provider=provider)
@app.post('/api/import')
@require_login
def import_legacy():
 d=request.get_json(silent=True) or {}; items=d.get('messages') or []
 if not isinstance(items,list): return jsonify(error='Invalid chat backup.'),400
 title=str(d.get('title') or 'Imported chat from original Switchboard')[:100]
 pid=secrets.token_urlsafe(9); now=time.time()
 with db() as c:
  c.execute('INSERT INTO projects VALUES(?,?,?,?)',(pid,title,now,now))
  for m in items:
   if not isinstance(m,dict) or m.get('role') not in ('user','assistant') or m.get('pending') or m.get('error'): continue
   content=str(m.get('content',''))[:120000]
   if content: c.execute('INSERT INTO messages(project_id,role,content,provider,model,created) VALUES(?,?,?,?,?,?)',(pid,m['role'],content,str(m.get('provider',''))[:40],str(m.get('model',''))[:120],now))
 return jsonify(id=pid,title=title)
@app.get('/api/backup')
@require_login
def backup():
 with db() as c:
  projects=[dict(x) for x in c.execute('SELECT id,title,created,updated FROM projects ORDER BY created').fetchall()]
  messages=[dict(x) for x in c.execute('SELECT project_id,role,content,provider,model,created FROM messages ORDER BY id').fetchall()]
 return jsonify(format='switchboard-backup-v1',projects=projects,messages=messages,created=time.time())
@app.post('/api/restore')
@require_login
def restore():
 d=request.get_json(silent=True) or {}
 if d.get('format')!='switchboard-backup-v1' or not isinstance(d.get('projects'),list) or not isinstance(d.get('messages'),list):
  return jsonify(error='This is not a valid Switchboard backup.'),400
 old_to_new={}; now=time.time()
 with db() as c:
  for item in d['projects']:
   if not isinstance(item,dict): continue
   old=str(item.get('id','')); pid=secrets.token_urlsafe(9)
   title=str(item.get('title') or 'Restored project')[:100]
   c.execute('INSERT INTO projects VALUES(?,?,?,?)',(pid,title,now,now)); old_to_new[old]=pid
  for m in d['messages']:
   if not isinstance(m,dict) or str(m.get('project_id','')) not in old_to_new: continue
   if m.get('role') not in ('user','assistant'): continue
   content=str(m.get('content',''))[:120000]
   if content:
    c.execute('INSERT INTO messages(project_id,role,content,provider,model,created) VALUES(?,?,?,?,?,?)',(old_to_new[str(m['project_id'])],m['role'],content,str(m.get('provider',''))[:40],str(m.get('model',''))[:120],float(m.get('created') or now)))
 return jsonify(ok=True,restored_projects=len(old_to_new))

@app.errorhandler(413)
def too_large(e): return jsonify(error='File too large. Maximum 15 MB.'),413
if __name__=='__main__':
 print('Switchboard by Vijay Dodiya running on port',PORT)
 if not APP_PASSWORD: print('WARNING: Set APP_PASSWORD before exposing this app to the internet.')
 app.run(host='0.0.0.0',port=PORT,debug=False)
