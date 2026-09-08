"""Re-key the personal-data columns under a new data key.

    python rekey.py            generate a new key, rewrite every encrypted column, store the key
    python rekey.py --dry-run  count the rows that would be rewritten

Order matters: the database is rewritten first, in one transaction (rekey_personal_data,
data/sql/009), and only then the new key is stored in the secret (AWS) or printed for `.env`
(local). Readers that cached the old key re-read the secret on their next decrypt
(datakey.decrypting). On AWS run it as a one-off task on the API image: `make rekey-aws`.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

from datakey import data_key
from dbconn import RotatingPool


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--new-key", default="", help="use this key instead of a generated one"
    )
    args = ap.parse_args()
    pool = RotatingPool(min_size=1, max_size=2)
    old = data_key()
    new = args.new_key or secrets.token_urlsafe(32)
    with pool.connection() as conn, conn.cursor() as cur:
        if args.dry_run:
            cur.execute(
                "SELECT (SELECT count(*) FROM registry WHERE beneficial_owner_enc IS NOT NULL),"
                " (SELECT count(*) FROM entities WHERE name_enc IS NOT NULL)"
            )
            r, e = cur.fetchone().values()
            print(f"would rewrite registry={r} entities={e}")
            return 0
        cur.execute("SELECT * FROM rekey_personal_data(%s, %s)", (old, new))
        r, e = cur.fetchone().values()
        conn.commit()
    print(f"rewrote registry={r} entities={e}")
    arn = os.getenv("DATA_KEY_SECRET_ARN")
    if arn:
        import boto3

        boto3.client(
            "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).put_secret_value(SecretId=arn, SecretString=new)
        print(f"new key stored in {arn}; readers re-read it on their next decrypt")
    else:
        print(f"set DATA_KEY={new} in .env and restart the stack", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
