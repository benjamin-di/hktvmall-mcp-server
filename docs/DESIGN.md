# Design record

Why the server looks the way it does. [README.md](../README.md) is the contract for users;
[hktvmall-site.md](hktvmall-site.md) holds the evidence about HKTVmall's endpoints. This file keeps the decisions
and the reasons behind them.

## Decisions

| Decision | Why |
|---|---|
| An MCP server over HKTVmall's own web endpoints, plain HTTP, no browser at runtime | There is no official API or partner programme. A browser agent is slow and token-heavy; plain HTTP with a signed-in session's cookies mints tokens and edits the basket. |
| The person pays; no order, payment, voucher, address, list or slot write exists in the code | The safety boundary, pinned by `tests/test_safety.py`; checkout stays on HKTVmall (`checkout_url`). |
| The result of a change is the basket read back afterwards | HKTVmall's write replies are not true (silent drops, silent caps, `quantityAdded` with no change). |
| Exact quantities per named line, never a full-basket replacement | The basket is shared live with HKTVmall's app and site; replacing it from a stale view would delete lines added there (the reason the UCP/ACP style `update_cart` was rejected). |
| Quantity 0 always via `remove_item_from_cart`; one repair pass when a read fails after a removal | `update_item_in_cart` with 0 leaves a zombie line that makes every basket read return a 403 page; removal repairs it. |
| Caps (15 per line for increases, HK$1,500 added per call) with `confirmed`; account context in people mode | Basket edits are reversible and move no money. An opaque account context prevents a prepared edit crossing an account switch; it does not lock basket contents or replace the quantity/value caps. |
| Money as `{cents, display}` | An integer for code and a display string for quoting; models misquote bare cents. |
| One batched search tool | One search request carries the whole list (20 queries, 0.14 s, ~100 KB with field selection). |
| The default account's session is the server's own file (`hktvmall-mcp login`), or an external snapshot | Signing in needs a person (challenges, one-time codes), so the person does it in a real browser window and the server keeps the cookies. Operators who already keep a browser session signed in can publish it as a snapshot instead; one strict parser validates both. |
| A snapshot is read-only and in memory: stat per call, re-parse on change, adopt only a greater generation | `JSESSIONID` is stable across reads and `access_token` is re-minted, so nothing learned during calls needs to outlive the process; a local copy only added failure modes. |
| A session the server owns is written back when HKTVmall replaces cookies, unless a newer login replaced the file | Nobody else refreshes it. Writers hold a file lock, and a login raises the generation, so a write-back never overwrites a newer sign-in. |
| Each new generation is checked signed in, on the home page, before its first basket call | Guests have baskets too; a signed-out session must never show a guest basket as the account's. |
| A missing or invalid session fails the session and the health check at once | One authority, no stale fallback to reason about. |
| stdio by default, HTTP only behind a bearer | A client on the same computer starts the server and owns it; over a network the bearer is the only gate, so the server refuses HTTP without one. |
| State changes re-read the file under a lock | Each MCP client on a computer starts its own server process; they share the budget, the pause and the session. |
| Order history is read-only and re-orders go line by line | Re-ordering is a common job, but HKTVmall's whole-order re-add adds on top of the basket and bypasses caps, closed-shop checks and the read-back. Only the order list and an order's page are reachable, and only item lines are parsed. |
| Price limits go to HKTVmall; links and delivery labels go to the agent cheaply | A filter on the top hits found nothing for "milk under HK$20" while hundreds exist, so the limit goes into HKTVmall's own `range`. Agents invented links and paraphrased delivery codes, so every product carries HKTVmall's own page address and label. |
| Slots, reviews, unit prices and order tracking as agent context | Slots come from the checkout's own two reads; only the delivery zone leaves the address list. Reviews are public, so the session never goes to that host. Unit prices are parsed from the pack size only when it is unambiguous, because titles drop multipliers. |
| Bugs are fixed at their cause | Request shapes are copied from HKTVmall's own client and pinned in tests; every authenticated call proves the sign-in after re-minting; outcomes know what was sent and take stock levels from the basket; an all-empty search is probed before it is believed; one field name has one shape across tools (a test walks every output). |
| Several people, one service object per account; the gateway's verified person selects it | Per-account objects make crossing over structural (an order cache, an error or a session cannot be shared by accident); the gateway's verified person, not a tool argument, selects the account, so no prompt can reach another basket. People come from the identity provider: a person's record is created on first contact, never from a server-side list. Pacing, the pause and a total budget stay shared because every account leaves from the same IP address. Sessions are sealed (AES-GCM, bound to the person). Each person logs in on HKTVmall's own page in a browser the server shows them; nothing about the login is automated. |
| Ownership, access and selection are separate | The owner retains credentials and grants access; the recipient explicitly selects a shopping account. Keeping an owned connection must not prevent using a shared basket. Selection changes no provider binding or basket contents, and never falls back. Tools accept an opaque context to detect stale writes, never an account selector. |
| New shares permit shopping; history is optional | Household shopping does not imply access to past purchases. One explicit history permission is enforced before live and cached history; no roles system is needed. Existing grants retain their documented scope during migration. |
| Durable selection and grant incarnations | Selection revisions fence A→B→A; grant revisions fence removal/reissue and scope changes. Version-2 controls make older routing code refuse. Stable legacy revisions allow interrupted migration without deleting identity, generation, budget or cache evidence. |
| Sharing guidance before connecting; owner controls remain visible during reconnects | Recipients only need their own identity-provider sign-in, so asking everyone to connect HKTVmall first would prevent sharing. The owner must still be able to remove access during a reconnect. Person IDs distinguish identical display names. |
| Links from HKTVmall's own page addresses; photos as product cards (MCP Apps) | `/hktv/en/p/{id}` only works as a redirect, and a model-assembled id can be mistyped, so every product carries HKTVmall's canonical `url`. Assistants cannot show image URLs in answers, so `hktv_show_products` draws the picks in an MCP Apps view: photos from HKTVmall's image servers only, no network connections, text-only rendering, links through the host. The gallery rides in the result's `_meta`, so the model's context does not grow. |
| Paste by typing the clipboard's text into the login window, from a small module beside noVNC's stock viewer | noVNC 1.6 swallows Ctrl+V on its canvas and has no clipboard API support; letting the browser's own paste event fire and typing the text key by key (as noVNC's on-screen keyboard does) needs no change to noVNC and no clipboard permission. `scripts/paste_e2e.py` checks it in real browsers in the release workflow. |
| Pushback pauses 5 minutes, 30 for a repeat within two hours | A single 403 during a brief hiccup should not stop shopping for half an hour; real blocking repeats and still gets the long pause. |
| Completed orders' items are kept on disk; a call reads at most 10 new order pages | Agents send several history calls at once and requests leave one at a time; ten order pages take 15-20 s. A completed order never changes, so its item lines (only the tool's fields) are cached per account; a queued call waits 90 s. |
| Open output schemas | A closed schema breaks cached MCP clients when a field is added. |

## Not tested, and why

- Rate limits under bursts: probing them would risk the account and the IP address it shares.
- Prompt injection through product text: content cannot be planted. Mitigated by the instructions, by treating
  merchant and customer text as data, and by writes that accept only product ids and quantities.

## Risks

- Unofficial endpoints change: the health check covers search, the basket, order history, delivery slots, checkout
  warnings and reviews, and fixtures pin the shapes the code reads.
- Session lifetime is HKTVmall's decision; an ended session is reported as `session_expired`.
- New silent behaviours: the read-back reports them as `adjusted` or `rejected`; add tests as they are found.

## Research basis

Code read for open-source shopping MCP servers (among them REWE, Albert Heijn, Picnic, Kroger, H-E-B and Amazon
servers), the Universal Commerce Protocol, the Agentic Commerce Protocol, Shopify's Storefront API and its MCP, and
the MCP tool-design guidance. Taken: per-change outcomes against a fresh read; exact-quantity semantics; batch
candidates "for you to choose"; typed schemas with read/write annotations; tests that enforce annotations and argument
descriptions; the split between rejected and adjusted changes; a checkout handoff URL; integer money with a display
string. Rejected: full-replacement cart updates (they delete lines added in the app), operation journals and approval
pages (they guard orders, which this server never places), confirm tokens, cart revisions, polling, local cart
mirrors and retries of non-idempotent writes.

## Explicit selection

Account selection intentionally replaces the earlier own-first / sole-share rule. The person chooses in the browser;
MCP admission stays with the gateway. One selected context keeps tool calls unambiguous while allowing a retained owned
connection and several shared offers. No local issuer, per-call account chooser, automatic fallback, credential
sharing, invitation service or general permissions framework is introduced.

The basket-update tool has an optional `account_context` input for single-account compatibility, required by hosted
people-mode enforcement. This is a breaking hosted behavior change: clients must refresh cached tool definitions
before using the new reader, and release notes must call it out. Source landing does not upgrade installed data.
The new controls require compatible readers/writers and forward recovery after adoption (see
[permissions](PERMISSIONS.md#upgrading-hosted-controls)).
