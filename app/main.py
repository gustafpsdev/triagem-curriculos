from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import sqlite3, re, csv, io, hashlib, hmac, secrets, json, os
from datetime import datetime, timedelta
from pypdf import PdfReader

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "triagem_v2.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="Triagem RH - V2", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(BASE / "app" / "static")), name="static")


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def password_hash(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def password_ok(password: str, stored: str):
    try:
        salt, digest = stored.split("$", 1)
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def init_db():
    conn = db(); cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        senha_hash TEXT NOT NULL,
        perfil TEXT NOT NULL DEFAULT 'RH',
        ativo INTEGER NOT NULL DEFAULT 1,
        criado_em TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessoes (
        token TEXT PRIMARY KEY,
        usuario_id INTEGER NOT NULL,
        expira_em TEXT NOT NULL,
        FOREIGN KEY(usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS vagas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        titulo TEXT NOT NULL,
        unidade TEXT DEFAULT '',
        area TEXT DEFAULT '',
        descricao TEXT DEFAULT '',
        experiencia_min_meses INTEGER NOT NULL DEFAULT 0,
        requisitos_obrigatorios TEXT DEFAULT '',
        requisitos_desejaveis TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ATIVA',
        criado_por INTEGER,
        criado_em TEXT NOT NULL,
        FOREIGN KEY(criado_por) REFERENCES usuarios(id)
    );
    CREATE TABLE IF NOT EXISTS candidatos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vaga_id INTEGER NOT NULL,
        nome TEXT DEFAULT '',
        sobrenome TEXT DEFAULT '',
        telefone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        cidade TEXT DEFAULT '',
        origem TEXT NOT NULL DEFAULT 'UPLOAD MANUAL',
        source_external_id TEXT DEFAULT '',
        experiencia_resumo TEXT DEFAULT '',
        meses_experiencia INTEGER,
        competencias TEXT DEFAULT '[]',
        score INTEGER,
        status_triagem TEXT NOT NULL DEFAULT 'AGUARDANDO PROCESSAMENTO',
        justificativa TEXT DEFAULT '',
        decisao_rh TEXT NOT NULL DEFAULT 'PENDENTE',
        observacao_rh TEXT DEFAULT '',
        criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL,
        FOREIGN KEY(vaga_id) REFERENCES vagas(id)
    );
    CREATE TABLE IF NOT EXISTS curriculos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidato_id INTEGER NOT NULL,
        nome_arquivo TEXT NOT NULL,
        caminho_arquivo TEXT NOT NULL,
        mime_type TEXT DEFAULT 'application/pdf',
        texto_extraido TEXT DEFAULT '',
        processamento_status TEXT NOT NULL DEFAULT 'PROCESSADO',
        erro_processamento TEXT DEFAULT '',
        recebido_em TEXT NOT NULL,
        FOREIGN KEY(candidato_id) REFERENCES candidatos(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS avaliacoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidato_id INTEGER NOT NULL,
        vaga_id INTEGER NOT NULL,
        score INTEGER,
        requisitos_atendidos TEXT DEFAULT '[]',
        requisitos_nao_atendidos TEXT DEFAULT '[]',
        desejaveis_encontrados TEXT DEFAULT '[]',
        justificativa TEXT DEFAULT '',
        motor TEXT NOT NULL DEFAULT 'REGRAS_V1',
        criado_em TEXT NOT NULL,
        FOREIGN KEY(candidato_id) REFERENCES candidatos(id) ON DELETE CASCADE,
        FOREIGN KEY(vaga_id) REFERENCES vagas(id)
    );
    ''')
    if not cur.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone():
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.local")
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            raise RuntimeError("Defina ADMIN_PASSWORD antes da primeira execução.")
        cur.execute("INSERT INTO usuarios(nome,email,senha_hash,perfil,criado_em) VALUES(?,?,?,?,?)",
                    ("Administrador RH", admin_email, password_hash(admin_password), "ADMIN", now()))
    conn.commit(); conn.close()

init_db()


def auth_user(request: Request):
    token = request.cookies.get("triagem_session")
    if not token:
        raise HTTPException(401, "Não autenticado")
    conn = db()
    row = conn.execute('''SELECT u.* FROM sessoes s JOIN usuarios u ON u.id=s.usuario_id
                          WHERE s.token=? AND s.expira_em>? AND u.ativo=1''', (token, now())).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "Sessão expirada")
    return dict(row)


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def first_match(pattern, text, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else ""


def guess_name(text: str):
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    blocked = {"curriculo", "currículo", "resume", "perfil profissional", "experiência", "experiencia"}
    for line in lines[:12]:
        low = line.lower()
        if any(b in low for b in blocked) or "@" in line or any(ch.isdigit() for ch in line):
            continue
        words = line.split()
        if 2 <= len(words) <= 6 and len(line) <= 70:
            return words[0], " ".join(words[1:])
    return "", ""


def estimate_months(text: str) -> int:
    years = [int(n) for n in re.findall(r"(\d+)\s*(?:anos?|ano)\b", text, re.I)]
    months = [int(n) for n in re.findall(r"(\d+)\s*(?:meses?|m[eê]s)\b", text, re.I)]
    total = sum(y * 12 for y in years) + sum(months)
    return min(total, 600)


def split_terms(s: str):
    return [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]


def evaluate(text: str, vaga):
    lower = text.lower()
    months = estimate_months(text)
    mandatory = split_terms(vaga["requisitos_obrigatorios"])
    desired = split_terms(vaga["requisitos_desejaveis"])
    met = [x for x in mandatory if x.lower() in lower]
    missing = [x for x in mandatory if x.lower() not in lower]
    desired_hits = [x for x in desired if x.lower() in lower]

    min_months = int(vaga["experiencia_min_meses"] or 0)
    if min_months > 0 and months == 0:
        status, base = "REVISÃO DO RH", 45
        reason = "Tempo de experiência não foi identificado com segurança no currículo."
    elif months < min_months:
        status, base = "NÃO ATENDE REQUISITO MÍNIMO", min(49, int((months / max(min_months, 1)) * 49))
        reason = f"Experiência identificada: {months} meses; mínimo da vaga: {min_months} meses."
    elif missing:
        status, base = "REVISÃO DO RH", 58
        reason = "Alguns requisitos obrigatórios não foram encontrados explicitamente."
    else:
        status, base = "APTO PARA ANÁLISE", 75
        reason = "Os requisitos mínimos foram identificados no currículo."

    exp_bonus = min(15, max(0, months - min_months) // 3) if months else 0
    desired_bonus = min(10, len(desired_hits) * 5)
    score = min(100, base + exp_bonus + desired_bonus)
    return {
        "months": months, "score": score, "status": status, "reason": reason,
        "met": met, "missing": missing, "desired_hits": desired_hits
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "app" / "static" / "index.html").read_text(encoding="utf-8")

@app.post("/api/auth/login")
def login(response: Response, email: str = Form(...), senha: str = Form(...)):
    conn = db(); user = conn.execute("SELECT * FROM usuarios WHERE lower(email)=lower(?) AND ativo=1", (email,)).fetchone()
    if not user or not password_ok(senha, user["senha_hash"]):
        conn.close(); raise HTTPException(401, "E-mail ou senha inválidos")
    token = secrets.token_urlsafe(36)
    exp = (datetime.now() + timedelta(hours=12)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessoes(token,usuario_id,expira_em) VALUES(?,?,?)", (token, user["id"], exp)); conn.commit(); conn.close()
    response.set_cookie("triagem_session", token, httponly=True, samesite="lax", max_age=43200)
    return {"ok": True, "usuario": {"nome": user["nome"], "perfil": user["perfil"]}}

@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("triagem_session")
    if token:
        conn = db(); conn.execute("DELETE FROM sessoes WHERE token=?", (token,)); conn.commit(); conn.close()
    response.delete_cookie("triagem_session")
    return {"ok": True}

@app.get("/api/auth/me")
def me(request: Request):
    u = auth_user(request)
    return {"id": u["id"], "nome": u["nome"], "email": u["email"], "perfil": u["perfil"]}

@app.get("/api/dashboard")
def dashboard(request: Request):
    auth_user(request); conn = db(); cur = conn.cursor()
    cards = {
        "vagas_ativas": cur.execute("SELECT COUNT(*) FROM vagas WHERE status='ATIVA'").fetchone()[0],
        "candidatos": cur.execute("SELECT COUNT(*) FROM candidatos").fetchone()[0],
        "aptos": cur.execute("SELECT COUNT(*) FROM candidatos WHERE status_triagem='APTO PARA ANÁLISE'").fetchone()[0],
        "revisao": cur.execute("SELECT COUNT(*) FROM candidatos WHERE status_triagem='REVISÃO DO RH'").fetchone()[0],
        "entrevistas": cur.execute("SELECT COUNT(*) FROM candidatos WHERE decisao_rh='CHAMAR PARA ENTREVISTA'").fetchone()[0],
    }
    por_origem = [dict(r) for r in cur.execute("SELECT origem, COUNT(*) total FROM candidatos GROUP BY origem ORDER BY total DESC").fetchall()]
    por_vaga = [dict(r) for r in cur.execute('''SELECT v.titulo, COUNT(c.id) total FROM vagas v LEFT JOIN candidatos c ON c.vaga_id=v.id
                                                GROUP BY v.id ORDER BY total DESC, v.titulo LIMIT 10''').fetchall()]
    recentes = [dict(r) for r in cur.execute('''SELECT c.id,c.nome,c.sobrenome,c.status_triagem,c.score,c.origem,c.criado_em,v.titulo vaga
                                                FROM candidatos c JOIN vagas v ON v.id=c.vaga_id ORDER BY c.id DESC LIMIT 8''').fetchall()]
    conn.close(); return {"cards": cards, "por_origem": por_origem, "por_vaga": por_vaga, "recentes": recentes}

@app.get("/api/vagas")
def list_vagas(request: Request):
    auth_user(request); conn = db(); rows = conn.execute("SELECT * FROM vagas ORDER BY id DESC").fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.post("/api/vagas")
def create_vaga(request: Request, titulo: str=Form(...), unidade: str=Form(""), area: str=Form(""), descricao: str=Form(""), experiencia_min_meses: int=Form(0), requisitos_obrigatorios: str=Form(""), requisitos_desejaveis: str=Form("")):
    u = auth_user(request); conn = db(); cur = conn.cursor()
    cur.execute('''INSERT INTO vagas(titulo,unidade,area,descricao,experiencia_min_meses,requisitos_obrigatorios,requisitos_desejaveis,criado_por,criado_em)
                   VALUES(?,?,?,?,?,?,?,?,?)''', (titulo,unidade,area,descricao,experiencia_min_meses,requisitos_obrigatorios,requisitos_desejaveis,u["id"],now()))
    conn.commit(); vid=cur.lastrowid; conn.close(); return {"id": vid}

@app.patch("/api/vagas/{vid}/status")
def vaga_status(vid: int, request: Request, status: str=Form(...)):
    auth_user(request)
    if status not in {"ATIVA","PAUSADA","ENCERRADA"}: raise HTTPException(400,"Status inválido")
    conn=db(); conn.execute("UPDATE vagas SET status=? WHERE id=?",(status,vid)); conn.commit(); conn.close(); return {"ok":True}

@app.get("/api/candidatos")
def list_candidatos(request: Request, vaga_id: int|None=None, status: str|None=None, origem: str|None=None, q: str|None=None):
    auth_user(request); conn=db()
    sql='''SELECT c.*,v.titulo vaga_titulo,v.unidade vaga_unidade FROM candidatos c JOIN vagas v ON v.id=c.vaga_id WHERE 1=1'''; params=[]
    if vaga_id: sql += " AND c.vaga_id=?"; params.append(vaga_id)
    if status: sql += " AND c.status_triagem=?"; params.append(status)
    if origem: sql += " AND c.origem=?"; params.append(origem)
    if q:
        sql += " AND (lower(c.nome||' '||c.sobrenome) LIKE ? OR lower(c.email) LIKE ? OR lower(c.telefone) LIKE ?)"
        qq=f"%{q.lower()}%"; params += [qq,qq,qq]
    sql += " ORDER BY c.id DESC"
    rows=conn.execute(sql,params).fetchall(); conn.close(); return [dict(r) for r in rows]

@app.get("/api/candidatos/{cid}")
def candidate_detail(cid:int, request:Request):
    auth_user(request); conn=db()
    c=conn.execute('''SELECT c.*,v.titulo vaga_titulo,v.unidade vaga_unidade,v.requisitos_obrigatorios,v.requisitos_desejaveis
                      FROM candidatos c JOIN vagas v ON v.id=c.vaga_id WHERE c.id=?''',(cid,)).fetchone()
    if not c: conn.close(); raise HTTPException(404,"Candidato não encontrado")
    cv=conn.execute("SELECT id,nome_arquivo,processamento_status,recebido_em FROM curriculos WHERE candidato_id=? ORDER BY id DESC LIMIT 1",(cid,)).fetchone()
    av=conn.execute("SELECT * FROM avaliacoes WHERE candidato_id=? ORDER BY id DESC LIMIT 1",(cid,)).fetchone(); conn.close()
    out=dict(c); out["curriculo"]=dict(cv) if cv else None; out["avaliacao"]=dict(av) if av else None; return out

@app.post("/api/candidatos/upload")
async def upload_candidate(request: Request, vaga_id:int=Form(...), origem:str=Form("UPLOAD MANUAL"), arquivo:UploadFile=File(...)):
    auth_user(request)
    if not arquivo.filename.lower().endswith(".pdf"): raise HTTPException(400,"Nesta versão, envie um PDF.")
    conn=db(); vaga=conn.execute("SELECT * FROM vagas WHERE id=?",(vaga_id,)).fetchone()
    if not vaga: conn.close(); raise HTTPException(404,"Vaga não encontrada")
    safe=f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}_{re.sub(r'[^A-Za-z0-9._-]','_',arquivo.filename)}"
    path=UPLOADS/safe; path.write_bytes(await arquivo.read())
    try:
        text=extract_pdf_text(path)
    except Exception as e:
        conn.close(); raise HTTPException(400,f"Não foi possível ler o PDF: {e}")
    nome,sobrenome=guess_name(text)
    email=first_match(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})",text)
    telefone=first_match(r"((?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?9?\d{4}[-\s]?\d{4})",text)
    result=evaluate(text,vaga)
    resumo=" ".join(text.split())[:700]
    cur=conn.cursor(); stamp=now()
    cur.execute('''INSERT INTO candidatos(vaga_id,nome,sobrenome,telefone,email,origem,experiencia_resumo,meses_experiencia,score,status_triagem,justificativa,criado_em,atualizado_em)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(vaga_id,nome,sobrenome,telefone,email,origem,resumo,result["months"],result["score"],result["status"],result["reason"],stamp,stamp))
    cid=cur.lastrowid
    cur.execute('''INSERT INTO curriculos(candidato_id,nome_arquivo,caminho_arquivo,texto_extraido,recebido_em) VALUES(?,?,?,?,?)''',(cid,arquivo.filename,safe,text,stamp))
    cur.execute('''INSERT INTO avaliacoes(candidato_id,vaga_id,score,requisitos_atendidos,requisitos_nao_atendidos,desejaveis_encontrados,justificativa,criado_em)
                   VALUES(?,?,?,?,?,?,?,?)''',(cid,vaga_id,result["score"],json.dumps(result["met"],ensure_ascii=False),json.dumps(result["missing"],ensure_ascii=False),json.dumps(result["desired_hits"],ensure_ascii=False),result["reason"],stamp))
    conn.commit(); conn.close(); return {"id":cid,"status":result["status"],"score":result["score"]}

@app.patch("/api/candidatos/{cid}/decisao")
def candidate_decision(cid:int, request:Request, decisao:str=Form(...), observacao:str=Form("")):
    auth_user(request)
    allowed={"PENDENTE","CHAMAR PARA ENTREVISTA","BANCO DE TALENTOS","DESCARTAR","CONTRATADO"}
    if decisao not in allowed: raise HTTPException(400,"Decisão inválida")
    conn=db(); conn.execute("UPDATE candidatos SET decisao_rh=?,observacao_rh=?,atualizado_em=? WHERE id=?",(decisao,observacao,now(),cid)); conn.commit(); conn.close(); return {"ok":True}

@app.get("/api/candidatos/{cid}/pdf")
def candidate_pdf(cid:int, request:Request):
    auth_user(request); conn=db(); row=conn.execute("SELECT caminho_arquivo,nome_arquivo FROM curriculos WHERE candidato_id=? ORDER BY id DESC LIMIT 1",(cid,)).fetchone(); conn.close()
    if not row: raise HTTPException(404,"Currículo não encontrado")
    return FileResponse(str(UPLOADS/row["caminho_arquivo"]), media_type="application/pdf", filename=row["nome_arquivo"])

@app.get("/api/export.csv")
def export_csv(request:Request):
    auth_user(request); conn=db(); rows=conn.execute('''SELECT c.id,v.titulo vaga,v.unidade,c.nome,c.sobrenome,c.telefone,c.email,c.origem,c.meses_experiencia,c.score,c.status_triagem,c.decisao_rh,c.criado_em
                                                        FROM candidatos c JOIN vagas v ON v.id=c.vaga_id ORDER BY c.id DESC''').fetchall(); conn.close()
    headers=["id","vaga","unidade","nome","sobrenome","telefone","email","origem","meses_experiencia","score","status_triagem","decisao_rh","criado_em"]
    buf=io.StringIO(); w=csv.writer(buf); w.writerow(headers)
    for r in rows: w.writerow([r[h] for h in headers])
    return StreamingResponse(iter([buf.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=candidatos.csv"})
