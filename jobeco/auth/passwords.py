from __future__ import annotations

import hashlib
import hmac
import os


PBKDF2_ALGO = "sha256"
DEFAULT_ITERATIONS = 250_000


def hash_password_pbkdf2(password: str, *, salt: str | None = None, iterations: int = DEFAULT_ITERATIONS) -> str:
  """
  Password hash format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
  """
  if salt is None:
    salt_bytes = os.urandom(16)
    salt_hex = salt_bytes.hex()
  else:
    salt_hex = salt.encode("utf-8").hex()

  pw_bytes = password.encode("utf-8")
  salt_bytes = bytes.fromhex(salt_hex)
  dk = hashlib.pbkdf2_hmac(PBKDF2_ALGO, pw_bytes, salt_bytes, iterations)
  return f"pbkdf2_sha256${iterations}${salt_hex}${dk.hex()}"


def verify_password_pbkdf2(password: str, password_hash: str) -> bool:
  try:
    algo, iter_str, salt_hex, hash_hex = password_hash.split("$", 3)
    if algo != "pbkdf2_sha256":
      return False
    iterations = int(iter_str)
    pw_bytes = password.encode("utf-8")
    salt_bytes = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALGO, pw_bytes, salt_bytes, iterations)
    return hmac.compare_digest(dk.hex(), hash_hex)
  except Exception:
    return False

