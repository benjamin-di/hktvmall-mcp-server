"""Commands for a server that hosts several people (``hktvmall-mcp admin ...``; in a container,
``docker exec hktvmall-mcp hktvmall-mcp admin ...``):

  invites              historical invites: label, connected account, revoked (no new signups)
  revoke PREFIX        withdraw an invite (its first 8+ characters from ``invites``)
  accounts             connected accounts: id, masked email, sign-in state
  disconnect ID        durably stop server access; retain ownership, budgets and caches

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
    if command == "invite":
        print("Invite signup is retired. Configure identity-provider access and reviewed account/management bindings.")
        return 2
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
            account = args[0]
            ok = account == "default" or account in accounts.ids() or account in accounts.bindings().values()
            if ok:
                epoch = accounts.control(account).suspend()
                accounts.disconnect_owned(account, epoch)
            print("disconnected" if ok else "no such account")
            return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
