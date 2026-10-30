"""Operator commands for a server that hosts several people (``hktvmall-mcp admin ...``; in a container,
``docker exec hktvmall-mcp hktvmall-mcp admin ...``):

  accounts             people: display name, masked email, connection state, sign-in, whom they share with
  disconnect PERSON    durably stop server access to PERSON's connection; keep their record, budgets and caches

People are added and removed in the identity provider (its group), never here; sharing is the owner's choice on
My connections. Never prints cookies.
"""

from __future__ import annotations

import json
import sys

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args else "help"
    if command in ("accounts", "disconnect"):
        from .server import build_accounts

        accounts = build_accounts(Settings.from_env())
        if command == "accounts" and not args:
            print(json.dumps(accounts.describe(), indent=2, ensure_ascii=False))
            return 0
        if command == "disconnect" and len(args) == 1:
            person = args[0]
            ok = person == "default" or person in accounts.known()
            if ok:
                epoch = accounts.control(person).suspend()
                accounts.disconnect_owned(person, epoch)
            print("disconnected" if ok else "no such person")
            return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
