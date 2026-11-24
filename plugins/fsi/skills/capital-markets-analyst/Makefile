.PHONY: setup test eval export-xlsx demo chat add-company mcp-server a2ui-card clean-dry-run

PYTHON ?= python3
CHAT_PORT ?= 8081

setup:
	bash scripts/bootstrap.sh --quick-start

test:
	$(PYTHON) -m pytest tests -v

eval:
	$(PYTHON) scripts/eval_runner.py

export-xlsx:
	$(PYTHON) scripts/model_exporter.py --book next27_flagship --scenario fed_up_50_oil_spike --output ./_output/sp500_read_the_fine_print_model.xlsx

demo:
	$(PYTHON) scripts/server.py --port 8080

# Chat with the agent in the ADK web page: http://127.0.0.1:$(CHAT_PORT)/dev-ui/?app=scripts
# Needs Application Default Credentials and the Vertex AI variables in the environment (see README).
# Sessions are kept in memory, so ADK stores no session files in this folder.
chat:
	$(PYTHON) -m google.adk.cli web --host 127.0.0.1 --port $(CHAT_PORT) --no-reload --session_service_uri memory:// --artifact_service_uri memory:// scripts

# Fetch the reported figures of companies outside the bundle from SEC EDGAR and keep them locally:
#   make add-company TICKERS="TSLA NFLX"      (needs SEC_EDGAR_USER_AGENT)
add-company:
	$(PYTHON) scripts/sec_lookup.py $(TICKERS)

# MCP App server over stdio (needs the `mcp` extra). An MCP host starts it; run it by hand only to debug.
mcp-server:
	$(PYTHON) scripts/mcp_server.py

# Print the A2UI v0.9 holding card for UAL as JSON Lines.
a2ui-card:
	$(PYTHON) scripts/a2ui_cards.py --card holding --ticker UAL

clean-dry-run:
	$(PYTHON) scripts/cleanup.py --dry-run
