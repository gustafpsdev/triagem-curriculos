# Automação de Triagem de Currículos

Aplicação web para cadastrar vagas, receber currículos em PDF, extrair informações e apoiar a triagem inicial por critérios configuráveis.

## Tecnologias

Python, FastAPI, SQLite, pypdf, JavaScript, HTML e CSS.

## Execução

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_EMAIL="admin@example.local"
export ADMIN_PASSWORD="uma-senha-forte"
uvicorn app.main:app --reload
```

No Windows, ative o ambiente com `.venv\Scripts\activate` e use `set` para definir as variáveis.

> Utilize somente currículos fictícios durante testes públicos. Arquivos enviados e o banco local são ignorados pelo Git.
