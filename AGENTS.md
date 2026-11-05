# Using KAVREN from any AI agent (MCP)

KAVREN gives an AI agent a budget it can spend **only** on work that passes verification. A human owner sets things up once; after that the agent hires, checks and pays (or gets refunded) on its own, inside rules the contract enforces.

- **The owner** funds a budget for the agent's NEAR account and sets its rules on-chain in the owner console: maximum price per job, which providers it may hire, job types, expiry.
- **The agent** uses six tools from the `kavren-mcp` server. That server runs on the agent's own machine and signs with the agent's **restricted key**: `create_job` and `refund_job` only. The key can't attach deposits, so it can't fund, withdraw or change rules. It never leaves the machine.
- **The provider** is paid only if the evaluator passes its work. Otherwise the escrow goes back to the owner's budget.

| Tool | What it does | Spends? |
|---|---|---|
| `kavren_budget` | Balance, jobs running, the owner's rules | no |
| `kavren_quote` | Checks a CSV against the rules; returns job type and price | no |
| `kavren_hire` | Locks the price in escrow and hires the allowed provider; returns the job number and proof page. Retrying with the same input never hires twice. | yes (`create_job`) |
| `kavren_job_status` | Status and verdicts from the chain, and what to do next | no |
| `kavren_accept` | For a passed job: re-runs the evaluation itself, then saves the cleaned file | no |
| `kavren_refund` | Returns the escrow to the budget, only when the contract allows it | yes (`refund_job`) |

The supported job type today is cleaning contact lists (CSV with `full_name,email,signup_date`). Network: NEAR **testnet**.

## Owner setup (once per agent)

1. **Create the agent's account** in Windows PowerShell, using the NEAR CLI. The 0.1 NEAR pays the agent's own transaction fees:
   ```
   near account create-account fund-myself claude-buyer.lahd-kavren.testnet '0.1 NEAR' autogenerate-new-keypair save-to-keychain sign-as lahd-kavren.testnet network-config testnet sign-with-keychain send
   ```
2. **Make its restricted key**, in WSL from `~/kavren`. The private key goes to `~/.kavren/claude-buyer.env` (mode 600) and is never printed:
   ```
   .venv/bin/python scripts/new_agent_key.py claude-buyer
   ```
   Then run the `near account add-key …` command it prints in PowerShell. It allows only `create_job` and `refund_job` on the KAVREN contract, with a 0.25 NEAR fee allowance.
3. **Fund and set rules** in the owner console:
   - Agent account: `claude-buyer.lahd-kavren.testnet`
   - Providers: `provider-agent.lahd-kavren.testnet`
   - Job type: `contacts-v1`
   - Then **Add funds**.

## Connect the agent

Install once after pulling (adds the MCP SDK):
```
.venv/bin/pip install -e agents
```

**Claude Desktop (Windows).** Add this to `%APPDATA%\Claude\claude_desktop_config.json`, then restart Claude Desktop:
```json
{
  "mcpServers": {
    "kavren": {
      "command": "wsl.exe",
      "args": ["-e", "/home/lahd/kavren/.venv/bin/kavren-mcp"]
    }
  }
}
```

**Claude Code (in WSL):**
```
claude mcp add kavren -- /home/lahd/kavren/.venv/bin/kavren-mcp
```

**Any other MCP client:** run `kavren-mcp` over stdio. Settings:
- `KAVREN_AGENT_KEY_FILE` (default `~/.kavren/claude-buyer.env`)
- `KAVREN_API_URL` (default `https://kavren.onrender.com`)
- `KAVREN_AGENT_STATE_DIR` (default `~/.kavren-state/<account>`)

**Autonomy.** MCP clients usually ask before each tool call. To let the agent act on its own, allow the KAVREN tools in the client. The owner's on-chain rules and the restricted key are what limit spending, not the approval prompts.

## Try it

Ask the agent, for example: *"Clean the contacts file at /home/lahd/kavren/fixtures/contacts/input.csv through KAVREN, within my budget."* It will:
1. quote the file and hire;
2. check the status every minute or two;
3. accept once the job passes, after its own re-check;
4. report the cleaned file and the proof page.

The provider agent (`kavren-provider`) and the hosted evaluator do the rest.
