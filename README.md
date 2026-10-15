# OFFERHUNT

OFFERHUNT is an AI-powered offer discovery, extraction, and proof-validation engine built with the Model Context Protocol (MCP v2), Google Gemini 2.5, Tavily Search, and Streamlit.

## Project Structure

```text
Offer-Hunt/
├── app.py                  # Main Streamlit web application entrypoint
├── agent/                  # Agent core, orchestrator, and prompt logic
├── mcp_tools/              # MCP tool definitions and servers
├── storage/                # SQLite persistence and data repositories
│   └── migrations/         # SQL migration scripts
├── reliability/            # Rate limiting, retry, and safety boundaries
├── tests/                  # Pytest test suite
├── data/                   # Seed files, catalogs, sample inputs
├── artifacts/              # Generated spreadsheets, reports, DB (offerhunt.db)
├── logs/                   # Structured execution and audit logs
├── scripts/                # Verification, smoke test, and utility scripts
├── .env.example            # Environment variables template
├── .gitignore              # Git ignore rules
├── requirements.txt        # Top-level dependencies
└── pyproject.toml          # Project configuration and packaging
```

## Setup & Configuration

1. **Virtual Environment**:
   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **API Keys Configuration**:
   Copy `.env.example` to `.env`:
   ```powershell
   Copy-Item .env.example .env
   ```
   Open `.env` and fill in your actual keys:
   - `GEMINI_API_KEY`: Google Gemini API key from [Google AI Studio](https://aistudio.google.com/)
   - `GEMINI_MODEL`: Model ID (e.g. `gemini-2.5-flash`)
   - `TAVILY_API_KEY`: Tavily Search API key from [Tavily](https://app.tavily.com/)

3. **Running the Application**:
   ```powershell
   streamlit run app.py
   ```
