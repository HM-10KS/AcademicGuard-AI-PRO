import io, os, re, json, ast, hashlib
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import fitz
from docx import Document
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from xml.sax.saxutils import escape

ROOT = Path(__file__).parent
app = FastAPI(title='AcademicGuard AI', version='1.0.0')
app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')
MAX_BYTES = int(os.getenv('MAX_UPLOAD_MB','8')) * 1024 * 1024
EXTENSIONS = {'.pdf','.docx','.txt','.py','.js','.ts','.java','.cs','.dart','.html','.css','.sql','.cpp','.c'}
CODE_EXT = {'.py','.js','.ts','.java','.cs','.dart','.html','.css','.sql','.cpp','.c'}

def client():
    url, key = os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key: raise HTTPException(503,'Server Supabase configuration missing')
    return create_client(url,key)

def get_user(authorization: str | None):
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(401,'Please sign in')
    token = authorization.split(' ',1)[1].strip()
    try:
        user = client().auth.get_user(token).user
        if not user: raise ValueError('Invalid session')
        return user.id
    except Exception:
        raise HTTPException(401,'Session invalid or expired')

def extract(filename, data):
    ext=Path(filename).suffix.lower()
    try:
        if ext=='.pdf':
            with fitz.open(stream=data,filetype='pdf') as doc:
                if len(doc)>200: raise HTTPException(413,'PDF too many pages')
                return '\n'.join(p.get_text() for p in doc)
        if ext=='.docx':
            doc=Document(io.BytesIO(data))
            return '\n'.join(p.text for p in doc.paragraphs)
        return data.decode('utf-8-sig')
    except HTTPException: raise
    except Exception: raise HTTPException(422,'Unable to read file. Text-based documents only.')

def normalize(text, code=False):
    text=text.lower()
    if code:
        text=re.sub(r'(?m)^\s*(#|//).*$', '', text)
        text=re.sub(r'\b[a-z_][a-z_0-9]*\b', 'id', text)
    return re.sub(r'\s+', ' ', text).strip()

def compare(text, other, is_code):
    a,b=normalize(text,is_code),normalize(other,is_code)
    if not a or not b: return 0
    try:
        v=TfidfVectorizer(analyzer='char',ngram_range=(3,5),max_features=30000)
        m=v.fit_transform([a[:100000],b[:100000]])
        cosine=float(cosine_similarity(m[0],m[1])[0,0])
    except ValueError: cosine=0
    sequence=SequenceMatcher(None,a[:12000],b[:12000],autojunk=True).ratio()
    return round(max(0,min(1,.8*cosine+.2*sequence))*100,2)

@app.get('/')
def home(): return FileResponse(ROOT/'static'/'index.html')

@app.get('/api/health')
def health(): return {'status':'ok','mode':'local similarity baseline','ai_model_enabled':False}

@app.get('/api/scans')
def scans(authorization: str | None = Header(default=None)):
    uid=get_user(authorization)
    result=client().table('scans').select('id,filename,file_type,score,matched_sources,created_at').eq('user_id',uid).order('created_at',desc=True).limit(100).execute()
    return {'scans':result.data}

@app.get('/api/scans/{scan_id}')
def scan_detail(scan_id:str, authorization: str | None = Header(default=None)):
    uid=get_user(authorization)
    try: result=client().table('scans').select('*').eq('id',scan_id).eq('user_id',uid).single().execute()
    except Exception: raise HTTPException(404,'Scan not found')
    return result.data

@app.post('/api/scan')
async def scan(file:UploadFile=File(...), category:str=Form('research'), authorization:str|None=Header(default=None)):
    uid=get_user(authorization)
    filename=Path(file.filename or '').name[:180]
    ext=Path(filename).suffix.lower()
    if ext not in EXTENSIONS: raise HTTPException(415,'Unsupported file extension')
    if category not in ('research','code'): raise HTTPException(400,'Invalid category')
    if (ext in CODE_EXT) != (category=='code') and ext not in ('.txt',):
        raise HTTPException(400,'File type does not match selected category')
    data=await file.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES: raise HTTPException(413,'File too large')
    if not data: raise HTTPException(400,'Empty file')
    text=extract(filename,data)
    if len(text.strip())<30: raise HTTPException(422,'Not enough extractable text (minimum 30 characters)')
    # Only compare within the signed-in user's own corpus to avoid leaking other students' data.
    db=client()
    existing=db.table('scans').select('id,filename,report').eq('user_id',uid).eq('file_type',category).order('created_at',desc=True).limit(100).execute().data
    matches=[]
    for row in existing:
        previous=(row.get('report') or {}).get('source_text','')
        if not previous: continue
        score=compare(text,previous,category=='code')
        if score>=15: matches.append({'source_id':row['id'],'filename':row['filename'],'similarity':score})
    matches.sort(key=lambda x:x['similarity'],reverse=True)
    top=matches[:10]
    report={'matches':top,'source_text':text[:100000], 'characters':len(text), 'method':'TF-IDF character n-grams + sequence similarity (baseline, not semantic AI)', 'disclaimer':'Similarity is not proof of academic misconduct. Human review required.'}
    score=top[0]['similarity'] if top else 0
    saved=db.table('scans').insert({'user_id':uid,'filename':filename,'file_type':category,'score':score,'matched_sources':len(matches),'report':report}).execute()
    return {'scan':{'id':saved.data[0]['id'],'filename':filename,'score':score,'matches':top,'characters':len(text)},'notice':'Only your own previously uploaded documents are compared. No AI detection is claimed.'}

@app.get('/api/scans/{scan_id}/pdf')
def download_report(scan_id:str,authorization:str|None=Header(default=None)):
    row=scan_detail(scan_id,authorization)
    out=io.BytesIO(); doc=SimpleDocTemplate(out)
    styles=getSampleStyleSheet(); story=[Paragraph('AcademicGuard AI — Similarity Report',styles['Title']),Spacer(1,16)]
    for label,value in [('File',row['filename']),('Similarity score',str(row['score'])+'%'),('Method','Local text/code similarity baseline'),('Notice','Similarity is not proof of misconduct. Human review is required.')]:
        story.append(Paragraph(escape(label+': '+value),styles['Normal']));story.append(Spacer(1,10))
    for match in row['report'].get('matches',[]):
        story.append(Paragraph(escape(f"Match: {match['filename']} — {match['similarity']}%"),styles['Normal']))
    doc.build(story);out.seek(0)
    return StreamingResponse(out,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="academicguard-{scan_id}.pdf"'})
