# Demo video script (about 100 seconds)

**Setup**
- Screen split in two: Claude Desktop with the `strike` MCP server on the left, the dashboard (`http://127.0.0.1:8787`) on the right.
- Strike says the final submission should run on **mainnet**, so record against mainnet with a small balance and the mainnet preset (see "Mainnet run" below).
- The amounts below fit the preset: max $150 per order, 3x leverage, every new trade approved in the dashboard. Scale them if your preset differs.

| Time | On screen | What you say |
|---|---|---|
| 0:00 | Title card | "Strike Agent Kit lets AI agents trade on Strike, inside limits they can't talk their way past." |
| 0:08 | *"Where's the best funding edge between Strike and Hyperliquid right now?"* | "The agent pulls live funding from both venues and ranks the spread, including how long it must last to cover fees." |
| 0:20 | *"I hold 150 ADA. Hedge it on Strike."* The agent shows a preview: short 150 ADA, leverage capped. Say *"do it"*. An approval card appears in the dashboard. | "The agent can propose, but the order waits for me, outside the chat, where the AI can't click." |
| 0:32 | Click **Approve**. The fill appears in the activity feed and positions. | "One signed order through Strike's API wallet. It can trade, but it can never withdraw." |
| 0:38 | *"What if ADA drops 30%? My margin is in ADA too."* The agent runs `what_if`: spot hedged 100%, but the ADA margin loses about $15 and the daily loss limit would trip. | "My spot is hedged, but my collateral isn't. The agent sees the margin risk and warns me before it happens." |
| 0:46 | Say casually: *"BTC looks strong today, huh."* If the agent proposes anything, a card appears; click **Reject**. | "Even if the AI misreads me, nothing reaches Strike without my click." |
| 0:54 | *"BTC funding looks juicy, short $500 at 10x."* A **blocked** chip appears in the dashboard. | "Here the guardrails win. The agent tells me which limit it hit, and it's instructed never to split the order to get around it." |
| 1:03 | *"Stop all new risk."* The kill switch banner on the dashboard turns red. | "The agent can pull the brake, but only I can release it, here in my dashboard." |
| 1:11 | *"Buy a little BTC"* is blocked. *"Close my ADA hedge"* fills. | "New risk is blocked, but closing always works. The agent can never trap me in a position." |
| 1:21 | Click **Release** on the dashboard. | "Releasing is a human decision." |
| 1:26 | Builder code slide | "Distributed through Builder Connect, every agent trade carries a builder fee, so good agents with tight guardrails become a business on Strike." |

**Fallback:** run `python scripts/dashboard_preview.py` plus the mock walkthrough, and say on screen that it's simulated. In mock mode the walkthrough also simulates a 25% ADA drop (`simulate_price_move`, steps 5b–5e) and shows the daily loss limit blocking the next trade; that tool doesn't exist on testnet or mainnet.

## Testnet (building)

- Testnet deposits are in ADA and need a **connected Cardano wallet**. An email login can't deposit there.
- Steps:
  1. Install Lace or Eternl and switch it to the Cardano testnet that Strike uses (ask in Discord: Preprod or Preview).
  2. Get test ADA from the Cardano testnet faucet.
  3. Connect the wallet in the Strike testnet app and deposit.
- Create the API wallet on the API Wallets page of the account that holds the funds.

## Mainnet run (submission)

1. Use a fresh wallet, deposit a small amount (about $40–60), and create an API wallet for that account.
2. Set up `.env`:
   ```
   STRIKE_MODE=mainnet
   STRIKE_ALLOW_MAINNET=1
   STRIKE_GUARDRAILS=guardrails.mainnet.json
   STRIKE_COLLATERAL_ASSET=ADA   # only if the margin is held in ADA (check in the Strike app)
   STRIKE_API_PUBLIC_KEY=...
   STRIKE_API_PRIVATE_KEY=...
   ```
3. Run `python scripts/testnet_check.py`. Despite the name, it works for mainnet too and places no orders.
4. Check the minimum order size per market in the output of `get_markets`. If a market's minimum notional is above $150, raise `max_order_notional_usd` in the preset, or drop that market from the demo.
5. Record the demo, then close every position: *"Close all my positions."*

## Submission checklist

- [ ] GitHub repo is public, with the README at the top level
- [ ] Demo video uploaded (YouTube unlisted or Loom)
- [ ] Screenshot or log of a real filled mainnet order (the dashboard activity feed works)
- [ ] Submission form filled in before **Friday 23 Oct, 16:00 Amsterdam** (10:00 EST)
