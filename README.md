# Automação de Triagem de Currículos

![Python](https://img.shields.io/badge/Python-3.11+-496B86?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-API-5C776F?logo=fastapi&logoColor=white) ![Status](https://img.shields.io/badge/status-em%20desenvolvimento-68717A)

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


## Arquitetura

```text
Interface web
     ↓
API FastAPI
     ├── Extração de PDF
     ├── Regras de pontuação
     └── Autenticação local
             ↓
          SQLite
```

Currículos enviados e o banco de dados permanecem apenas no ambiente local e não são versionados.

## Licença

Distribuído sob a licença MIT. Consulte o arquivo [LICENSE](LICENSE).
