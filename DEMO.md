# Demo video script (about 2 minutes)

Recorded on **Strike testnet** with the default `guardrails.json` (max $2,500 per order, 5x leverage, every new trade approved in the dashboard). The hackathon rules don't require mainnet; see "Mainnet run" below for the optional mainnet version.

The script uses **ETH**, not ADA. On 22 Oct the testnet ADA and SOL order books sat about 20% away from the mark, so market orders there get cancelled for slippage (the kit warns about this in every preview). BTC and ETH books are tight. Verified live on 22 Oct at 21:47: an ETH protection rule armed in the dashboard fired 0.05 s later and filled, and the close filled too.

## Tools

No camera needed: a screen recording with your voice.

| What | Tool |
|---|---|
| Screen + voice | macOS screen recording: **Cmd+Shift+5** → *Record Entire Screen*, *Options* → your microphone. Stop from the menu bar. |
| Voice | AirPods or the built-in mic, in a quiet room. Easier: record the screen silently, then add the voice in iMovie. |
| Editing | **iMovie** (free): cut the waits while Claude thinks, add the title card. |
| Slides | Keynote or an iMovie title: the product name at the start, the Builder Connect message at the end. |
| Upload | YouTube, **Unlisted**. |

## Setup

- **Left half:** Claude Desktop with the `strike` MCP server. **Right half:** the dashboard at `http://127.0.0.1:8787` (hover the green window button → tile left/right). Zoom the browser once or twice (Cmd +) so the numbers are readable.
- **Clean slate:** rename `.strike-agent` to `.strike-agent-<date>` in the kit folder, then restart Claude Desktop (Cmd+Q, reopen). Fresh activity feed, rules and day PnL.
- **Notifications:** turn Focus / Do Not Disturb **off**, and close apps that pop up their own. Do one throwaway run of steps 6–7 first, so macOS asks for notification permission off camera (it may list it under "Script Editor"). Then clean slate again.
- **New chat** in Claude Desktop, so the earlier conversation isn't in view.
- **Trigger price for step 6:** just before recording, ask *"What's the ETH price?"* and pick a round number a little **above** it (mark $2,377 → say "$2,400"). The condition is then already met, so the rule fires the moment you arm it. Say so on screen.

## Shot list

| # | Type in Claude | On screen | What you say |
|---|---|---|---|
| 1 | *(title card)* | | "Strike Agent Kit lets AI agents trade on Strike, inside limits they can't talk their way past." |
| 2 | *"Where's the best funding edge between Strike and Hyperliquid right now?"* | Funding table | "It pulls live funding from both venues and ranks the spread, including how long it must last to cover fees. These are testnet numbers." |
| 3 | *"I hold 0.04 ETH. Hedge it on Strike."* Then *"Do it."* | Preview (short 0.04 ETH, about $95, leverage capped), then an approval card in the dashboard | "The agent can propose, but the order waits for me, outside the chat, where the AI can't click." |
| 4 | | Click **Approve**. The fill appears in the activity feed and positions. | "One signed order through Strike's API wallet. It can trade, but it can never withdraw." |
| 5 | *"What if ETH drops 30%?"* | `what_if`: the spot ETH loses about $29, the short gains about the same | "Without the hedge I'd lose about $29; with it, the net is close to zero. The agent checks the downside before it happens." |
| 6 | *"I've got 0.01 more ETH in another wallet. If ETH drops below $[price], cover all 0.05."* | Pink card: **If ETH falls below $…: hedge 0.05 ETH** | "Now the agent stands guard. It proposes a rule; I arm it once." |
| 7 | | Click **Arm rule**. Green **Protection rule fired** banner, a Mac notification, and the extra 0.01 ETH short in positions. | "To show it firing, I set the line just above today's price. No second click: the kit placed the hedge itself, still inside every limit I set." |
| 8 | *"Is my ETH protected?"* | The agent answers from `list_protection_rules`: fired, sold 0.01 ETH at the fill price | "And it can tell me exactly what it did, and why." |
| 9 | *"Short $5,000 of BTC at 10x."* | **Blocked** chip in the dashboard (order cap and leverage cap) | "Here the guardrails win. The agent tells me which limit it hit, and it's instructed never to split the order to get around it." |
| 10 | *"Then raise my max order to $4,000."* Then *"Lower my daily loss limit to $200."* | Approval card **Max per order $2,500 → $4,000**: click **Reject**. The lower loss limit applies at once. | "The agent can tighten its own limits, but loosening them is my call, in the dashboard." |
| 11 | *"Stop all new risk."* | Kill switch banner turns red | "The agent can pull the brake, but only I can release it, here in my dashboard." |
| 12 | *"Buy a little BTC."* Then *"Close my ETH hedge."* | Blocked, then the close fills | "New risk is blocked, but closing always works. The agent can never trap me in a position." |
| 13 | | Click **Release** in the dashboard | "Releasing is a human decision." |
| 14 | *(closing slide)* | | "Distributed through Builder Connect, every agent trade carries a builder fee, so good agents with tight guardrails become a business on Strike." |

Each answer from Claude takes a few seconds; cut the waits in iMovie. If the video runs long, cut step 8 first, then step 2.

**After recording:** `git checkout guardrails.json` (step 10 changed the daily loss limit), and check that no ETH position is left open.

Notes:
- In step 9, "10x" alone doesn't block: the kit lowers leverage to the 5x cap and says so. The $5,000 size is what trips the $2,500 order cap.
- An old 0.11 SOL long may show in positions; the testnet SOL book is too far from the mark to close it. It's harmless.

**Fallback:** run `python scripts/dashboard_preview.py` plus the mock walkthrough (`python scripts/demo_walkthrough.py`), and say on screen that it's simulated. In mock mode the walkthrough also simulates price moves (`simulate_price_move`): step 5a2 shows a protection rule firing by itself on a real crossing, and 5b–5d show the hedge offsetting a 25% drop. That tool doesn't exist on testnet or mainnet.

## Testnet (building)

- Testnet deposits are in ADA and need a **connected Cardano wallet** (Lace or Eternl on **Preprod**). An email login can't deposit there.
- Get test ADA from the Cardano testnet faucet, connect the wallet at testnet.strikefinance.org and deposit.
- Create the API wallet under **Tools → API** in the account that holds the funds.

## Mainnet run (optional)

On mainnet the ADA order book is real, so the script can use ADA (e.g. *"I hold 150 ADA"*, rule *"cover all 650"*) with the mainnet preset's amounts: max $150 per order, 3x leverage. In step 9 say *"short $500 at 10x"*, in step 10 *"raise my max order to $600"* and *"lower my daily loss limit to $10"*.

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
6. Step 10 changes `guardrails.mainnet.json`. Put it back afterwards with `git checkout guardrails.mainnet.json`.

## Submission checklist

- [ ] GitHub repo is public, with the README at the top level
- [ ] Demo video uploaded (YouTube unlisted or Loom)
- [ ] Screenshot or log of a real filled order on testnet or mainnet (the dashboard activity feed works)
- [ ] Submission form filled in before **Friday 23 Oct, 16:00 Amsterdam** (10:00 EST)
