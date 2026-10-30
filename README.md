# hktvmall-mcp

Read the [permissions model](docs/PERMISSIONS.md).

An [MCP](https://modelcontextprotocol.io) server for [HKTVmall](https://www.hktvmall.com), Hong Kong's online mall
and supermarket. It lets an AI assistant (Claude, ChatGPT, Cursor, ...) search HKTVmall for a whole shopping list,
compare prices and unit prices, show product cards with photos, read your order history and set quantities in your
real basket. **You pay yourself, on HKTVmall:** the server cannot place, pay for or change an order.

> Unofficial. Not affiliated with or endorsed by HKTVmall or Hong Kong Television Network Limited. HKTVmall has no
> public API; this server uses the same endpoints HKTVmall's own website uses, gently and only when you ask. Use it
> with your own account for your own shopping; HKTVmall's terms and conditions apply to you.

**Requirements**

- A connection from **Hong Kong**: HKTVmall answers connections from elsewhere with errors.
- An HKTVmall account for the basket and order history (search works without one).
- [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 if needed), or Docker for a server.
- Google Chrome for signing in (or Chromium, see [Signing in](#signing-in)).

## Quick start

1. **Sign in once.** A browser window opens on HKTVmall's own login page; log in as usual (HKTVmall's own checks
   included). It closes once you are signed in, and the session is saved for the server.

   ```sh
   uvx --from "hktvmall-mcp[login]" hktvmall-mcp login
   ```

2. **Add the server to your client.** It runs over stdio, started by the client:

   - Claude Code: `claude mcp add hktvmall -- uvx hktvmall-mcp`
   - Claude Desktop, Cursor and most other clients: add to the client's MCP configuration

     ```json
     {
       "mcpServers": {
         "hktvmall": {
           "command": "uvx",
           "args": ["hktvmall-mcp"]
         }
       }
     }
     ```

3. **Ask.** "Put milk, a dozen eggs and some bananas in my HKTVmall basket", "What did I order last month? Add the
   same toilet paper again", "Which of these olive oils is cheapest per 100 ml?".

These commands run the latest release from PyPI ([`hktvmall-mcp`](https://pypi.org/project/hktvmall-mcp/)); write
`hktvmall-mcp==X.Y.Z` to stay on a release, or `--from git+https://github.com/benjamin-di/hktvmall-mcp-server` for the
latest code.

## Tools

| Tool | What it does |
|---|---|
| `hktv_search(queries[1..25], zone, per_query)` | The whole shopping list in one HKTVmall request; a few lean candidates per item (id, title, title_zh, brand, pack, price, availability, seller, delivery label such as "Next Day Delivery", `price_per_unit` per 100 ml, 100 g, roll or piece when the pack size is unambiguous, `review_count`, `url`). `max_price_cents` is sent to HKTVmall as a price range. Supermarket zone by default, widened per item when it has nothing; at most 100 products per call. |
| `hktv_get_products(ids)` | Current price, stock, delivery, `price_per_unit`, `review_count`, `summary` (shelf life, packing), `origin`, `image` and `url`; `not_found` lists ids HKTVmall does not sell right now. |
| `hktv_show_products(items[{id, note}] 1..12, title)` | The assistant's picks as product cards (photo gallery, price, unit price, rating, delivery, seller, the assistant's note, a link), drawn by clients that support MCP Apps (claude.ai, ChatGPT); other clients get the same data as text. |
| `hktv_delivery_slots()` | HKTVmall's next delivery slots for your first address, the fee, `free_from` and the slot already chosen. You pick a slot at checkout. |
| `hktv_reviews(id, limit 1..5, rating)` | Public reviews: average, count, breakdown by rating and recent comments; nothing about the reviewers. |
| `hktv_get_cart()` | The basket as HKTVmall shows it: lines, delivery groups with fees and `amount_to_free_delivery`, totals (as HKTVmall's order summary computes them), `checkout_url`, and HKTVmall's checkout warnings in `notes`. |
| `hktv_update_cart(changes[{id, quantity}], confirmed)` | Exact quantities (0 removes); each change's outcome (`applied`, `adjusted` with the reason such as `stock_limit`, or `rejected` with the reason such as `out_of_stock` or `store_closed`) is read back from HKTVmall. Raising a line above 15 or adding over HK$1,500 in one call changes nothing (`requires_buyer_review`) unless the person agreed (`confirmed`). |
| `hktv_recent_orders(limit 1..50, offset, items)` | Your order history, newest first: number, date, status, total, deliveries and the items (id, title, quantity, line total). Pages back through the whole history with `offset`. Read-only: never the address, payment or vouchers. Re-ordering goes through `hktv_update_cart`, line by line. |
| `hktv_status(check)` | Sign-in state, today's budget, any pause and the latest unresolved error; on a server that hosts several people, `connection` says whose HKTVmall account calls use (`owner`, `shared`). `check=true` runs a cached health check against HKTVmall (for monitoring). |
| `hktv_connect()` | The person's My connections link (`url`), where they connect, reconnect or share HKTVmall; on a local server, `how` tells them the login command. Starts nothing. |

Money is `{"cents": 12990, "display": "HK$129.90"}`. Errors are `{"error": {code, next_step, retryable, action}}`:
the `next_step` text tells the assistant what to do, and `action` is `retry_later`, `human_account_check`,
`repair_local_infrastructure` or `none`. A call that changed anything never returns an error: it reports each
change's outcome.

## Safety

Enforced in code and pinned by `tests/test_safety.py`:

- **No money moves.** No endpoint for placing, paying for, changing or cancelling an order, and none for vouchers,
  credit, addresses, profiles, wish lists or delivery-slot changes, exists in the code. You check out on HKTVmall.
- **Only HKTVmall.** Requests go only to `www.hktvmall.com`, its search service and its public review service, to an
  allowlist of paths; redirects that leave those hosts are refused. The product cards load photos from HKTVmall's
  image servers only and connect nowhere.
- **The truth is the basket.** HKTVmall's replies to basket changes are not always true (an item silently dropped,
  a quantity silently capped), so every change is read back and reported as HKTVmall then shows it.
- **Gentle.** One request at a time, at most about three a second, a daily request budget, and a pause of 5 minutes
  (30 when it repeats) whenever HKTVmall pushes back.
- **Your account only.** Each call acts for the caller's own HKTVmall connection, or the one a household member
  chose to share with them, never one named in a tool argument. Product names, descriptions and reviews are treated
  as data, not instructions.

## Privacy

Everything stays on the computer that runs the server, in its state folder (`~/.local/state/hktvmall-mcp` on Linux,
`~/Library/Application Support/hktvmall-mcp` on macOS, `%LOCALAPPDATA%\hktvmall-mcp` on Windows, `/state` in the
container; `HKTVMALL_STATE_DIR` overrides it):

| File | What it holds |
|---|---|
| `session.json` | Your HKTVmall sign-in (cookies) and the browser identity it was made with. **Treat it like a password.** |
| `browser/` | The sign-in window's browser profile, so HKTVmall recognises the browser next time. |
| `state.json`, `shared.json` | Today's request budget, any pause, unresolved errors and health-check results. |
| `orders.json` | The item lines of completed orders already read (id, title, quantity, line total), so the history loads quickly. |
| `people/<person>/` | On a server that hosts several people: each person's connection record (owner, whom they share with, disconnect state), encrypted session (`session.bin`), budget and order cache. |

Nothing is sent anywhere except HKTVmall. Logs (stderr) never contain cookies, search terms or HKTVmall's error text;
a basket change HKTVmall did not apply as asked is logged with its product id and quantities, for diagnosis. The
server never reads your address or payment details (the delivery-slot lookup reads only the delivery zone of your
first address).
`hktvmall-mcp logout` deletes the session and the browser profile.

## Signing in

`hktvmall-mcp login` opens HKTVmall's login page in your installed Google Chrome, using a separate profile in the
state folder. You do every step yourself; nothing is clicked or typed for you. When HKTVmall's own home page says you
are signed in, the session is saved and the window closes. A running server picks up the new session with its next
call.

- No Chrome? Run `patchright install chromium`, then `hktvmall-mcp login --browser chromium`, both the same way as
  in the [Quick start](#quick-start) (`uvx --from "hktvmall-mcp[login]" ...`).
- HKTVmall decides how long a session lasts. When it ends, tools return `session_expired`: sign in again.
- `hktvmall-mcp status` shows whether the saved session is signed in, without asking HKTVmall anything.

## Running it as a server

For remote clients (claude.ai, ChatGPT and the Claude and ChatGPT apps reach connectors over HTTPS) or a shared
home server, run it over streamable HTTP. It refuses to start without a bearer token:

```sh
docker run -d --name hktvmall-mcp -p 127.0.0.1:8000:8000 \
  -e MCP_BEARER_TOKEN="$(openssl rand -hex 32)" \
  -v hktvmall-state:/state \
  ghcr.io/benjamin-di/hktvmall-mcp-server:latest
```

or `MCP_BEARER_TOKEN=... uvx hktvmall-mcp serve --port 8000`.
The MCP endpoint is `/mcp`;
`/health` is open for health checks (the image's `HEALTHCHECK` runs `hktvmall-mcp health`). Put it behind your own
HTTPS reverse proxy to reach it from outside. In a private single-account setup, the bearer
(`Authorization: Bearer <token>`) grants that account's complete backend access. For hosted/shared use, keep it
server-only in an authenticating gateway; clients authenticate to the gateway separately as described below.

The server still needs a signed-in session in its state folder. Sign in with `hktvmall-mcp login` on a computer with
a screen in the same network and copy that computer's `session.json` into the server's state folder (the volume
above); the server adopts it on its next call. HKTVmall decides how long a session lasts; if it ends sooner on the
server, sign in again the same way.

### Hosting several people

Behind an authenticating gateway, the server hosts several people. The gateway admits MCP clients, supplies the
server-only bearer and sets the header named by `HKTVMALL_TRUSTED_ACCOUNT_HEADER` to the caller's verified,
administrator-owned person id (lowercase letters, digits, `_` and `-`). Keep the backend bearer private to the
gateway; a request without it, or with a missing, repeated or malformed person, gets `not_connected` and never
another person's account.

**People come from the identity provider.** Whoever the gateway admits (for example, members of an `mcp-hktvmall`
group) has a record: the first call or Connections sign-in of a person creates `people/<person>/`. There is no
per-server list to keep; removing someone from the group removes their access. A folder whose connection record is
missing (a lost or replaced volume) refuses rather than starting over.

**Connect your own account, or use one shared with you.** My connections is `<HKTVMALL_PUBLIC_URL>/connections/`, which
`hktv_connect` returns. They authenticate through the configured OIDC provider and log in on HKTVmall's own page in a
browser window the server shows them; nothing about the login is automated. The first HKTVmall account a person
connects stays theirs: a reconnect must be the same account, and an HKTVmall account another person connected is
refused, so nobody signs in to someone else's account.

**Sharing is the owner's choice.** A household often shops in one basket. The person who connected it can share it
with others on My connections: their assistants then use the same basket and order history, without the password,
and cannot reconnect, disconnect or share it. Nothing is shared until the owner says so, a person uses at most one
account (their own, or the one shared with them), and calls never fall back to another account: when the owner's
connection needs signing in again, members get `owner_reconnect` naming the owner. Unsharing, a member's **Stop
using** and the owner's **Disconnect** apply from the next call. `hktv_status.connection` names the account in use.

To share a household account:

1. Everyone opens **My connections** from `hktv_connect` and signs in with their own identity-provider login.
   This makes them appear in the sharing list; recipients do not need to connect an HKTVmall account.
2. The account owner connects HKTVmall, then returns to **My connections → Shared with** and presses **Share**
   beside the recipient. Person IDs beside display names distinguish people with the same name.
3. The recipient opens My connections to see **Using [owner]'s HKTVmall account**. Their assistant now uses that
   basket and order history. The owner can **Stop sharing**; the recipient can **Stop using**.

Sharing controls belong to the owner and remain available during a reconnect. Before connecting, the page explains
where sharing will appear. If someone is missing from the list, they must open My connections once or make an
admitted MCP call. If they have their own connection already, it cannot be replaced with a shared one, even after
Disconnect: the account binding is retained. Single-account/stdio servers have no sharing page; hosted sharing
requires people mode and the browser routes below, supported since 0.14.

Register exactly `<HKTVMALL_PUBLIC_URL>/connections/callback` with the OIDC provider; require its signed group and the
administrator-controlled person claim. The first explicit Connect, Disconnect or Share pins the person's issuer and
immutable subject; status reads never do. Connect and Share require a fresh authentication (within five minutes).
Every page, form, viewer asset and WebSocket checks the person; forms carry the record's epoch, so a stale form
changes nothing. A link, MCP token or forwarded header cannot substitute for the browser sign-in. Missing OIDC
settings refuse browser-enabled HTTP startup. Connecting grants no new assistant permissions; browser Sign out ends
only that browser session.

The login service (`HKTVMALL_LOGIN_URL`, `HKTVMALL_LOGIN_TOKEN`) shows HKTVmall's own page through noVNC and
automates no credential entry. A wrong or failed sign-in keeps the accepted session; closing the page keeps a
resumable window, and a completed save continues without the page. `scripts/paste_e2e.py` verifies signed synthetic
OIDC, owner isolation, the real Xvfb/x11vnc/noVNC viewer and clipboard input, page-close recovery, saving, sharing
and stopping, disconnect and logout. It never contacts HKTVmall.

Disconnect persists a barrier before cancelling windows or deleting the sealed session. It blocks new calls, cached
clients, cookie refresh and keep-alive across restarts; the record, its sharing, budgets and caches stay. Reconnect
advances the epoch and session generation. `hktvmall-mcp admin accounts` lists people (masked emails, state, sharing)
and `hktvmall-mcp admin disconnect PERSON` uses the same disconnect path.

Forward only GET `/connections/`, `/connections/status`, `/connections/login`, `/connections/callback`, POST
`/connections/start`, `/connections/disconnect`, `/connections/share`, `/connections/unshare`,
`/connections/leave`, `/connections/logout`, and the `/connect/<id>` viewer shapes. Strip Authorization and
forwarded identity headers on browser routes; preserve Cookie and Origin. Keep `/mcp` behind gateway admission.
Releases before 0.14 read operator account and manager maps (`HKTVMALL_CONNECTIONS_ACCOUNTS_FILE`,
`HKTVMALL_CONNECTIONS_MANAGERS_FILE`) and a default-account browser backend (`HKTVMALL_DEFAULT_LOGIN_URL`); these
settings now refuse startup, and images before 0.14 are not rollback targets once people have records.

Pasting works in the login window: Ctrl+V (Cmd+V on a Mac) types the clipboard's text into HKTVmall's page, so a
password manager's copy can be used (a small add-on to noVNC's viewer from
[signin-window](https://github.com/benjamin-di/signin-window), the library the login window is built on; up to 1000
characters the window's keyboard has, which covers ASCII; the text is never logged or kept). On a phone, noVNC's
keyboard button opens the phone's keyboard, which pastes as usual. Passkeys do not work in a remote window: log in
with a password or a code.

Run the login browser on a private network next to the server, never published to the internet: it listens on port
8080, refuses to start without `HKTVMALL_LOGIN_TOKEN` (at least 32 characters, the same value the server gets) and
holds at most `HKTVMALL_LOGIN_SLOTS` (2) logins at a time, each ended after `HKTVMALL_LOGIN_MINUTES` (15)
unfinished. The server's `HKTVMALL_LOGIN_URL` names it by host and port, such as `http://hktvmall-mcp-login:8080`.

### Session snapshot (for operators)

Instead of its own `session.json`, the default account can read a session that another program keeps signed in and
publishes as a file: set `HKTVMALL_SESSION_SNAPSHOT` (an absolute path, mounted read-only) and
`HKTVMALL_EXPECTED_EMAIL` (the account it must belong to). The server never writes it; it adopts a file only when
its `generation` is greater than the one it holds, and checks HKTVmall's home page before the first basket call of
each generation. The file is JSON, at most 512 KiB:

```json
{"schema": 1, "target": "hktvmall", "generation": 3, "email": "you@example.com",
 "user_agent": "Mozilla/5.0 ...", "verified_at": "2030-01-01T00:00:00+00:00",
 "cookies": [{"name": "JSESSIONID", "value": "...", "domain": "www.hktvmall.com", "path": "/",
              "secure": true, "expires": null}]}
```

`expires` is Unix seconds or null; it must contain a `JSESSIONID` for `www.hktvmall.com`. Symlinks, files that are
not regular files and timestamps in the future are rejected. `session.json` uses the same format.

## Configuration

| Variable | Default | |
|---|---|---|
| `MCP_BEARER_TOKEN` | required for `serve` | one or more tokens (comma-separated, at least 16 characters each): the default account's, or the gateway's when hosting several people |
| `HKTVMALL_STATE_DIR` | per-user folder (see [Privacy](#privacy)); `/state` in the image | sessions, budgets, pause, order cache |
| `HKTVMALL_DAILY_BUDGET` | 2000 | the default account's HKTVmall requests per day (resets 00:00 Hong Kong time) |
| `HKTVMALL_KEEPALIVE_SECONDS` | 21600 | how often an idle session is refreshed (re-mints its token and reads the basket) |
| `HKTVMALL_SESSION_SNAPSHOT`, `HKTVMALL_EXPECTED_EMAIL` | unset | read the default account's session from a published file ([above](#session-snapshot-for-operators)) |
| `HKTVMALL_SESSION_KEY` | unset | 32 random bytes, base64: encrypts each person's session; required when hosting several people |
| `HKTVMALL_ACCOUNT_DAILY_BUDGET` | 500 | each person's account's requests per day (shared by its members) |
| `HKTVMALL_TOTAL_DAILY_BUDGET` | 4000 | all accounts together per day |
| `HKTVMALL_TRUSTED_ACCOUNT_HEADER` | unset | Hosting several people: the exact gateway-set header with the verified person id, accepted only with the backend bearer |
| `HKTVMALL_PUBLIC_URL` | unset | Fixed public HTTPS base for My connections and its callback |
| `HKTVMALL_CONNECTIONS_ISSUER`, `HKTVMALL_CONNECTIONS_CLIENT_ID` | required with browser sign-in | Fixed OIDC issuer and registered client |
| `HKTVMALL_CONNECTIONS_GROUP` | `mcp-hktvmall` | Required signed group for My connections |
| `HKTVMALL_CONNECTIONS_ACCOUNT_CLAIM` | `person_id` | Administrator-controlled signed person id (the same value the gateway sends); never an email or display name |
| `HKTVMALL_CONNECTIONS_CONTROLS_DIR` | state directory `/connection-controls` | The default account's owner/disconnect record, shared with its snapshot custodian (single-account servers only) |
| `HKTVMALL_LOGIN_URL`, `HKTVMALL_LOGIN_TOKEN` | unset | the login browser service (`http://host:port` on a private network) and its bearer |
| `LOG_LEVEL` | INFO | |

## Troubleshooting

| Code | What to do |
|---|---|
| `not_signed_in` | Nothing connected yet: open the `hktv_connect` link (on a local server, run `hktvmall-mcp login`), or ask a household member to share their connection. |
| `connection_disconnected` | Server access was deliberately stopped; reconnect on My connections. |
| `session_expired` | HKTVmall ended the sign-in: reconnect on My connections, or run `hktvmall-mcp login` again locally (a snapshot needs a newer generation). |
| `owner_reconnect` | The connection is shared with you and its owner, named in the message, must reconnect it. |
| `not_connected` | The gateway did not name a person this server accepts; the administrator decides who has access. |
| `account_mismatch` | HKTVmall shows a different account than the saved one: sign in again. |
| `session_source_missing` / `session_source_invalid` | The session snapshot is missing, unreadable, for another account or malformed: check its publisher, mount and `HKTVMALL_EXPECTED_EMAIL`. A malformed `session.json`: sign in again. Hosting several people: a person's record under `state/people/` is unreadable (`hktvmall-mcp admin accounts` shows which); restore it from backup. |
| `blocked` | HKTVmall pushed back; calls resume after the pause (`hktv_status.paused_until`). |
| `budget_exhausted` | Today's budget is spent; it resets at 00:00 Hong Kong time. |
| `upstream_error` | HKTVmall or the network failed; retry in a minute. From outside Hong Kong, every call fails this way. |
| `schema_changed` | HKTVmall changed a page or API the server reads. Check for a newer release, or [open an issue](https://github.com/benjamin-di/hktvmall-mcp-server/issues). |
| `basket_unreadable` | HKTVmall cannot show the basket (for example after a zero-quantity line added elsewhere): open the basket in HKTVmall's app or site and remove the broken line. |
| `outcome_unknown` | A change was sent but could not be read back: check the basket before changing anything again. |

## How it works

HKTVmall has no public API, so the server speaks to the endpoints its website uses, over plain HTTP, with the
cookies of a browser session you signed in to. [docs/hktvmall-site.md](https://github.com/benjamin-di/hktvmall-mcp-server/blob/main/docs/hktvmall-site.md) records what is known
about those endpoints and their quirks; [docs/DESIGN.md](https://github.com/benjamin-di/hktvmall-mcp-server/blob/main/docs/DESIGN.md) explains the decisions. When HKTVmall
changes its site, calls fail with `schema_changed` until the server is updated.

## Development

See [CONTRIBUTING.md](https://github.com/benjamin-di/hktvmall-mcp-server/blob/main/CONTRIBUTING.md). Tests never contact HKTVmall.

## License

[MIT](https://github.com/benjamin-di/hktvmall-mcp-server/blob/main/LICENSE).

Optional operator-owned account upkeep for a single-account server is documented in
[Account maintenance](docs/MAINTENANCE.md).
