"""Commands for a server that hosts several people (``hktvmall-mcp admin ...``; in a container,
``docker exec hktvmall-mcp hktvmall-mcp admin ...``):

  invite LABEL         a new invite code for one friend, and what to send them (shown once; only its hash is kept)
  invites              invites: label, connected account, revoked
  revoke PREFIX        withdraw an invite (its first 8+ characters from ``invites``)
  accounts             connected accounts: id, masked email, sign-in state
  disconnect ID        delete a connected account's session (they can connect again with their invite)

Never prints cookies or invite codes other than the one just created.
"""

from __future__ import annotations

import json
import sys

from .config import Settings
from .invites import Invites


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args else "help"
    settings = Settings.from_env()
    invites = Invites(settings.state_dir)
    if command == "invite" and args:
        code = invites.create(" ".join(args))
        base = settings.public_url or "<HKTVMALL_PUBLIC_URL>"
        print(f"Invite code: {code}\nConnector:   {base}/mcp")
        print("In Claude or ChatGPT, add a custom connector with that address and press Connect; enter the code")
        print("when asked, then log in to HKTVmall as usual. The code works for the first account that uses it.")
        return 0
    if command == "invites":
        print(json.dumps(invites.describe(), indent=2, ensure_ascii=False))
        return 0
    if command == "revoke" and len(args) == 1:
        ok = invites.revoke(args[0])
        print("revoked" if ok else "no single invite matches")
        return 0 if ok else 1
    if command in ("accounts", "disconnect"):
        from .server import build_accounts

        accounts = build_accounts(settings)
        if command == "accounts":
            print(json.dumps(accounts.describe(), indent=2, ensure_ascii=False))
            return 0
        if len(args) == 1:
            ok = accounts.remove(args[0])
            print("disconnected" if ok else "no such account")
            return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
