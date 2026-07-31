import base64
import hashlib

from cryptography.fernet import Fernet
from flask import current_app


def _cipher():
    raw = current_app.config["SECRET_KEY"].encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_secret(value):
    return _cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value):
    return _cipher().decrypt(value.encode("utf-8")).decode("utf-8")
