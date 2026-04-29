debater: python -m src.debater_agent start
judge:   python -m src.judge_agent start
orch:    uvicorn src.orchestrator:app --host 0.0.0.0 --port 8000
web:     python -m http.server 5173 --directory web
