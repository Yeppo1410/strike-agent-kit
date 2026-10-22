# Demo video script (about 2 minutes)

**Setup**
- Screen split in two: Claude Desktop with the `strike` MCP server on the left, the dashboard (`http://127.0.0.1:8787`) on the right.
- Record on **testnet** (the default `guardrails.json`) or, optionally, on mainnet with a small balance and the mainnet preset (see "Mainnet run" below). The hackathon rules don't require mainnet.
- The amounts below fit the mainnet preset: max $150 per order, 3x leverage. On testnet (`guardrails.json`: max $2,500 per order, 5x) use the testnet amounts in the note under the table.

| Time | On screen | What you say |
|---|---|---|
| 0:00 | Title card | "Strike Agent Kit lets AI agents trade on Strike, inside limits they can't talk their way past." |
| 0:08 | *"Where's the best funding edge between Strike and Hyperliquid right now?"* | "The agent pulls live funding from both venues and ranks the spread, including how long it must last to cover fees." |
| 0:20 | *"I hold 150 ADA. Hedge it on Strike."* The agent shows a preview: short 150 ADA, leverage capped. Say *"do it"*. An approval card appears in the dashboard. | "The agent can propose, but the order waits for me, outside the chat, where the AI can't click." |
| 0:32 | Click **Approve**. The fill appears in the activity feed and positions. | "One signed order through Strike's API wallet. It can trade, but it can never withdraw." |
| 0:38 | *"What if ADA drops 30%?"* The agent runs `what_if`: the 150 ADA in my wallet loses about $10, the short hedge gains about the same, net close to zero and well inside the daily loss limit. | "Without the hedge I'd lose about $10; with it, the net is close to zero. The agent checks the downside before it happens, not after." |
| 0:46 | *"I've got 500 more ADA in another wallet. If ADA drops below [price], cover all 650."* The agent proposes a protection rule; a pink card appears: **If ADA falls below $… : hedge 650 ADA**. | "Now the agent stands guard. It proposes a rule; I arm it once." |
| 0:54 | Click **Arm rule**. The rule fires within seconds: a green **Protection rule fired** banner, a Mac notification, and the extra 500 ADA short in positions. | "No second click. The kit watched the price and placed the hedge itself, still inside every limit I set." |
| 1:02 | *"Is my ADA protected?"* The agent answers from `list_protection_rules`: fired, sold 500 ADA at the fill price. | "And it can tell me exactly what it did, and why." |
| 1:08 | *"BTC funding looks juicy, short $500 at 10x."* A **blocked** chip appears in the dashboard. | "Here the guardrails win. The agent tells me which limit it hit, and it's instructed never to split the order to get around it." |
| 1:17 | *"Then raise my max order to $600."* An approval card appears: **Max per order $150 → $600**. Click **Reject**. Then *"Lower my daily loss limit to $10."* It applies at once and the dashboard shows the new limit. | "The agent can tighten its own limits, but loosening them is my call, in the dashboard." |
| 1:27 | *"Stop all new risk."* The kill switch banner on the dashboard turns red. | "The agent can pull the brake, but only I can release it, here in my dashboard." |
| 1:35 | *"Buy a little BTC"* is blocked. *"Close my ADA hedge"* fills. | "New risk is blocked, but closing always works. The agent can never trap me in a position." |
| 1:45 | Click **Release** on the dashboard. | "Releasing is a human decision." |
| 1:50 | Builder code slide | "Distributed through Builder Connect, every agent trade carries a builder fee, so good agents with tight guardrails become a business on Strike." |

**Protection rule on camera:** a real crossing can take a long time, so pick **[price] a hair above today's ADA mark** (check it in the dashboard or the Strike app, e.g. mark $0.2281 → say "$0.23"). The condition is then already met, the agent will say the rule fires as soon as you arm it, and it does. That's honest to say on screen: "to show it firing, I set the line just above today's price." Before recording, make sure the 0:20 hedge (150 ADA) filled, so the rule only adds the other 500. The first Mac notification may need permission (macOS may list it under "Script Editor"), so trigger one test rule before recording.

**Testnet amounts:** at 1:08 say *"short $5,000 of BTC at 10x"* so the $2,500 order cap blocks it (over the leverage cap alone, the kit lowers leverage to 5x instead of blocking). At 1:17 say *"raise my max order to $4,000"*, then *"lower my daily loss limit to $200"*, and afterwards `git checkout guardrails.json`. Testnet funding rates are flat defaults, so keep the 0:08 funding beat short or say the numbers are testnet.

If the video runs long, cut 1:02 ("Is my ADA protected?") first, then the funding beat at 0:08.

**Fallback:** run `python scripts/dashboard_preview.py` plus the mock walkthrough, and say on screen that it's simulated. In mock mode the walkthrough also simulates ADA drops (`simulate_price_move`): step 5a2 shows a protection rule firing by itself on a real crossing, and 5b–5d show the hedge offsetting a 25% drop. That tool doesn't exist on testnet or mainnet.

## Testnet (building)

- Testnet deposits are in ADA and need a **connected Cardano wallet**. An email login can't deposit there.
- Steps:
  1. Install Lace or Eternl and switch it to the Cardano testnet that Strike uses (ask in Discord: Preprod or Preview).
  2. Get test ADA from the Cardano testnet faucet.
  3. Connect the wallet in the Strike testnet app and deposit.
- Create the API wallet on the API Wallets page of the account that holds the funds.

## Mainnet run (optional)

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
6. The 1:17 beat changes `guardrails.mainnet.json` (daily loss limit $10). Put it back afterwards with `git checkout guardrails.mainnet.json`.

## Submission checklist

- [ ] GitHub repo is public, with the README at the top level
- [ ] Demo video uploaded (YouTube unlisted or Loom)
- [ ] Screenshot or log of a real filled order on testnet or mainnet (the dashboard activity feed works)
- [ ] Submission form filled in before **Friday 23 Oct, 16:00 Amsterdam** (10:00 EST)
